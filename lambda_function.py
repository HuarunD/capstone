import json
import boto3
import base64
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pypdf import PdfReader
from io import BytesIO
from botocore.config import Config

bedrock = boto3.client(
    "bedrock-runtime",
    region_name="us-east-1",
    config=Config(
        read_timeout=300,
        connect_timeout=10,
        retries={"max_attempts": 2}
    )
)

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Smaller chunks = less JSON output per call = stays under token limit
CHUNK_SIZE = 3500

SYSTEM_PROMPT = """
You are an expert estate planning document analyst.

Extract structured information from this section of an estate planning document.

CRITICAL RULES:
- Return ONLY valid JSON — no preamble, no markdown fences, no explanation
- Keep ALL text values under 30 words
- Evidence quotes must be 15-30 words
- Include maximum 5 items per array
- Only extract what is explicitly present in this section

Use EXACTLY this schema:

{
 "trust_name": "",
 "document_type": "",
 "governing_state": "",
 "date_executed": "",
 "plain_english_summary": "",
 "advisor_flags": [{"flag": "", "evidence": {"location": "", "quote": ""}, "justification": {"brief": "", "verbose": ""}}],
 "key_provisions": [{"text": "", "evidence": {"location": "", "quote": ""}}],
 "people": {
   "grantors": [{"name": "", "evidence": {"location": "", "quote": ""}}],
   "spouse": {"name": "", "evidence": {"location": "", "quote": ""}},
   "children": [{"name": "", "birth_year": null, "evidence": {"location": "", "quote": ""}}],
   "grandchildren": [{"name": "", "birth_year": null, "evidence": {"location": "", "quote": ""}}],
   "initial_trustees": [{"name": "", "type": "", "evidence": {"location": "", "quote": ""}}],
   "successor_trustees": [{"name": "", "type": "", "order": 1, "evidence": {"location": "", "quote": ""}}],
   "other_beneficiaries": [{"name": "", "relationship": "", "evidence": {"location": "", "quote": ""}}]
 },
 "trusts": [{"name": "", "type": "", "primary_beneficiary": "", "funding_mechanism": "", "income_distribution": "", "principal_distribution": "", "evidence": {"location": "", "quote": ""}}],
 "distribution_events": [{"trigger": "", "beneficiary": "", "asset_description": "", "amount_or_pct": "", "distribution_type": "", "conditions": "", "evidence": {"location": "", "quote": ""}}],
 "specific_bequests": [{"beneficiary": "", "amount": "", "type": "", "condition": "", "evidence": {"location": "", "quote": ""}}],
 "validation_notes": ""
}

ADDITIONAL NOTES:
For "advisor_flags", "brief" section of "justification" includes a brief half-sentence on why the item is flagged.
While "verbose" section contains a more detailed explanation in 2-4 sentences.

Do NOT wrap output in markdown code fences. Return raw JSON only.
"""

SUMMARY_SYSTEM_PROMPT = """
You are an expert estate planning document analyst.
Summarize the trust document using ONLY the provided facts. Do not infer or add missing information.
Return ONLY the final summary text. No explanations, no JSON, no headings.

Format:
Write 3 short sentences in formal, objective language
Be precise and concise. Avoid subjective terms (e.g., "appears," "comprehensive")
Do not repeat information
All in one concise paragraph

Sentence order:
Trust type, grantor, jurisdiction
Lifetime distribution rule
A very brief description on key provisions (e.g., marital trust, GST/dynasty trust)
A very brief description of post-death distribution flow.

Then add a bullet list:
Each briefly describes a party, amount, and distribution trigger requirement, in the order of post-death distribution.
Also include relevant information regarding distribution trigger, including date of birth and their relationship to the grantor if applicable.
Combine beneficiaries if their distribution terms, timing, and conditions are similar.

Strict rules:
Use ONLY explicit facts
Preserve names and numbers exactly
"""

# ─────────────────────────────────────────────
# DOCUMENT SANITIZATION
# Strips signature blocks, notarial boilerplate, and other content
# that causes the LLM to produce malformed JSON output.
# ─────────────────────────────────────────────

def sanitize_document_text(text):
    if not text:
        return text

    # ── Large-document boilerplate stripping ──────────────────────────────
    # Strip table of contents: everything before the first real Article/Section
    # with substantive content (the TOC lists sections without their text).
    toc_end = re.search(
        r'\n(Article One\b|ARTICLE ONE\b|Article\s+1\b)',
        text,
        re.IGNORECASE
    )
    if toc_end and toc_end.start() > 500:
        # Only strip if the TOC is substantial (>500 chars before real content)
        text = text[toc_end.start():]

    # Strip "general trustee powers" article — it's standard boilerplate that
    # spans many pages and adds no unique information per-document.
    # We keep the signature/execution block that follows it.
    powers_match = re.search(
        r'\n(Article\s+(?:Eleven|Twelve|XI|XII)\b[\s\S]{0,80}?(?:Powers?|Authority))',
        text,
        re.IGNORECASE
    )
    if powers_match:
        before_powers = text[:powers_match.start()]
        after_powers  = text[powers_match.start():]
        # Re-attach signature block if present after the powers article
        sig_match = re.search(
            r'\n(?:IN WITNESS WHEREOF|SIGNATURE PAGE|EXECUTED\b|TRUSTMAKER\b)',
            after_powers,
            re.IGNORECASE
        )
        text = before_powers + (after_powers[sig_match.start():] if sig_match else "")

    # ── Signature / notarial boilerplate ─────────────────────────────────
    # Remove electronic /s/ signature lines
    text = re.sub(r'^/s/[^\n]*', '', text, flags=re.MULTILINE)

    # Remove notarial acknowledgement blocks ("STATE OF ...\n...\n) SS:")
    text = re.sub(r'STATE OF [A-Z ]+\n[\s\S]{0,300}?\) SS:', '', text)

    # Remove "COUNTY OF ..." notary lines
    text = re.sub(r'^COUNTY OF [A-Z ]+\n', '', text, flags=re.MULTILINE)

    # Remove "My Commission Expires ..." lines
    text = re.sub(r'^My Commission Expires[^\n]*', '', text, flags=re.MULTILINE)

    # Remove "Commission Number ..." lines
    text = re.sub(r'^Commission Numb(?:er)?[^\n]*', '', text, flags=re.MULTILINE)

    # Remove "THIS INSTRUMENT PREPARED BY ..." footer
    text = re.sub(r'^THIS INSTRUMENT PREPARED BY[\s\S]{0,400}?(?=\n\n|\Z)', '', text, flags=re.MULTILINE)

    # Remove "I Affirm, under the penalties for perjury ..." redaction notice
    text = re.sub(r'I Affirm,? under the penalties[\s\S]{0,300}?Attorney at Law[^\n]*', '', text)

    # Remove bare "Schedule A\n$10.00" funding placeholders (not real bequests)
    text = re.sub(r'^Schedule [A-Z]\s*\n\s*\$10(?:\.00)?\s*$', '', text, flags=re.MULTILINE)

    # Remove witness sections
    text = re.sub(r'^\(WITNESSES\)[^\n]*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^WITNESS my hand and Notarial Seal[^\n]*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\(Seal\)[^\n]*', '', text, flags=re.MULTILINE)

    # Remove lone single-digit lines (notarial artifacts like "1\n) SS:")
    text = re.sub(r'^\s*\d\s*$', '', text, flags=re.MULTILINE)

    # Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


# ─────────────────────────────────────────────
# PDF TEXT EXTRACTION
# ─────────────────────────────────────────────

def extract_pdf_text(pdf_base64):
    pdf_bytes = base64.b64decode(pdf_base64)
    reader = PdfReader(BytesIO(pdf_bytes))
    pages = []
    for i, page in enumerate(reader.pages):
        page_text = page.extract_text()
        if page_text:
            pages.append(f"[Page {i + 1}]\n{page_text}")
    return sanitize_document_text("\n\n".join(pages))


# ─────────────────────────────────────────────
# SPLIT DOCUMENT INTO CHUNKS
# ─────────────────────────────────────────────

def split_into_chunks(text, chunk_size=CHUNK_SIZE):
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size

        if end >= len(text):
            chunks.append(text[start:])
            break

        break_point = text.rfind("\n\n", start, end)
        if break_point == -1 or break_point <= start:
            break_point = text.rfind("\n", start, end)
        if break_point == -1 or break_point <= start:
            break_point = text.rfind(" ", start, end)
        if break_point == -1 or break_point <= start:
            break_point = end

        chunks.append(text[start:break_point])
        start = break_point

    return [c.strip() for c in chunks if c.strip()]


# ─────────────────────────────────────────────
# CALL BEDROCK — ANALYSIS
# ─────────────────────────────────────────────

def call_bedrock(prompt):
    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={
            "maxTokens": 4096,
            "temperature": 0
        }
    )
    return response["output"]["message"]["content"][0]["text"]


# ─────────────────────────────────────────────
# CALL BEDROCK — SUMMARY ONLY
# ─────────────────────────────────────────────

def call_bedrock_summary(facts):
    prompt = f"Write a plain-English summary of this estate planning document based on these facts:\n\n{facts}"
    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SUMMARY_SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={
            "maxTokens": 1000,
            "temperature": 0
        }
    )
    return response["output"]["message"]["content"][0]["text"].strip()


# ─────────────────────────────────────────────
# PARSE JSON SAFELY — HARDENED VERSION
#
# The LLM sometimes produces:
#   1. Markdown fences (```json ... ```)
#   2. Explanatory text before the JSON object
#   3. Explanatory text after the closing brace
#   4. Unescaped special characters inside string values
#      (e.g. /s/ signatures, ) SS: notarial artifacts)
#
# Strategy:
#   a. Strip markdown fences
#   b. Try direct parse
#   c. Extract the largest {...} block using start-of-first-{ + end-of-last-}
#   d. If still failing, attempt to repair common single-char issues
# ─────────────────────────────────────────────

def _repair_json(text):
    """
    Attempt lightweight repairs on near-valid JSON:
    - Replace unescaped control characters inside strings
    - Replace curly single/double quotes with ASCII equivalents
    """
    # Replace smart quotes
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2013', '-').replace('\u2014', '--')

    # Replace unescaped newlines inside JSON string values
    # This is a rough heuristic: replace \n that appear between two non-{ characters
    # inside what looks like a string value
    text = re.sub(r'(?<=": ")([^"]*?)\n([^"]*?)(?=")', lambda m: m.group(1) + ' ' + m.group(2), text)

    return text


def parse_json(text):
    text = text.strip()

    # Step 1: Strip markdown fences
    if text.startswith("```"):
        # Remove opening fence (with optional language tag)
        text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
        # Remove closing fence
        text = re.sub(r'\n?```\s*$', '', text)
        text = text.strip()

    # Step 2: Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Step 3: Extract outermost {...} block
    # Use first { and LAST } to handle trailing explanatory text
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        candidate = text[start:end]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # Step 4: Try repair on the extracted candidate
        repaired = _repair_json(candidate)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    # Step 5: Truncation recovery — the model ran out of tokens mid-output.
    # Find the last valid key-value boundary and close all open structures.
    if start != -1:
        raw = text[start:]
        # Walk backwards from the end, trying progressively shorter truncations
        # until we find a point we can close cleanly.
        for trim_end in range(len(raw), max(len(raw) - 500, 0), -1):
            fragment = raw[:trim_end].rstrip().rstrip(",").rstrip()
            # Count open braces/brackets to determine what needs closing
            depth_brace = fragment.count("{") - fragment.count("}")
            depth_bracket = fragment.count("[") - fragment.count("]")
            if depth_brace < 0 or depth_bracket < 0:
                continue
            # If we're mid-string (odd number of unescaped quotes), close the string first
            # Simple heuristic: close with empty string + close all open structures
            closing = ""
            # If last non-whitespace char suggests we're inside a string value, close it
            stripped = fragment.rstrip()
            if stripped and stripped[-1] not in ('"', '}', ']', 'l', 'e'):  # null, true, false end in l/e
                closing += '"'  # close any open string
            closing += "]" * depth_bracket
            closing += "}" * depth_brace
            candidate = fragment + closing
            try:
                result = json.loads(candidate)
                print(f"Warning: JSON was truncated; recovered partial result.")
                return result
            except json.JSONDecodeError:
                continue

    raise ValueError(
        f"No valid JSON found in model response. "
        f"Response preview: {text[:300]!r}"
    )


# ─────────────────────────────────────────────
# ANALYZE A SINGLE CHUNK
# ─────────────────────────────────────────────

def analyze_chunk(chunk_text, chunk_num, total_chunks, prospect_section=""):
    prompt = f"""Analyze the following section ({chunk_num} of {total_chunks}) of an estate planning document and extract structured data.
Note: This is a partial section — only extract what is present in this section. {prospect_section}

DOCUMENT SECTION:
{chunk_text}
"""
    raw = call_bedrock(prompt)
    return parse_json(raw)


# ─────────────────────────────────────────────
# DEDUPLICATE A LIST BY A KEY
# ─────────────────────────────────────────────

def dedup_by_key(items, key):
    seen = set()
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        val = item.get(key, "")
        if val and val not in seen:
            seen.add(val)
            result.append(item)
        elif not val:
            result.append(item)
    return result


def dedup_people_by_name(items):
    seen = set()
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "").strip().lower()
        if name and name not in seen:
            seen.add(name)
            result.append(item)
        elif not name:
            result.append(item)
    return result


# ─────────────────────────────────────────────
# PYTHON-BASED MERGE (no Bedrock needed)
# ─────────────────────────────────────────────

def python_merge(chunk_results):
    """Merge all chunk results in Python — fast, no token limits."""

    merged = {
        "trust_name": "",
        "document_type": "",
        "governing_state": "",
        "date_executed": "",
        "plain_english_summary": "",
        "advisor_flags": [],
        "key_provisions": [],
        "people": {
            "grantors": [],
            "spouse": {},
            "children": [],
            "grandchildren": [],
            "initial_trustees": [],
            "successor_trustees": [],
            "other_beneficiaries": [],
        },
        "trusts": [],
        "distribution_events": [],
        "specific_bequests": [],
        "validation_notes": "",
    }

    all_summaries = []
    all_validation = []

    for chunk in chunk_results:
        if not isinstance(chunk, dict):
            continue

        # Scalar fields — take first non-empty value
        for field in ["trust_name", "document_type", "governing_state", "date_executed"]:
            if not merged[field] and chunk.get(field):
                merged[field] = chunk[field]

        # Collect summaries to combine later
        if chunk.get("plain_english_summary"):
            all_summaries.append(chunk["plain_english_summary"])

        if chunk.get("validation_notes"):
            all_validation.append(chunk["validation_notes"])

        # Combine arrays
        merged["advisor_flags"].extend(chunk.get("advisor_flags") or [])
        merged["key_provisions"].extend(chunk.get("key_provisions") or [])
        merged["trusts"].extend(chunk.get("trusts") or [])
        merged["distribution_events"].extend(chunk.get("distribution_events") or [])
        merged["specific_bequests"].extend(chunk.get("specific_bequests") or [])

        # Merge people
        p = chunk.get("people") or {}
        merged["people"]["grantors"].extend(p.get("grantors") or [])
        merged["people"]["children"].extend(p.get("children") or [])
        merged["people"]["grandchildren"].extend(p.get("grandchildren") or [])
        merged["people"]["initial_trustees"].extend(p.get("initial_trustees") or [])
        merged["people"]["successor_trustees"].extend(p.get("successor_trustees") or [])
        merged["people"]["other_beneficiaries"].extend(p.get("other_beneficiaries") or [])

        # Spouse — take first non-empty
        if not merged["people"]["spouse"] and p.get("spouse") and isinstance(p["spouse"], dict) and p["spouse"].get("name"):
            merged["people"]["spouse"] = p["spouse"]

    # Deduplicate arrays
    merged["advisor_flags"] = dedup_by_key(merged["advisor_flags"], "flag")
    merged["key_provisions"] = dedup_by_key(merged["key_provisions"], "text")
    merged["trusts"] = dedup_by_key(merged["trusts"], "name")
    merged["distribution_events"] = dedup_by_key(merged["distribution_events"], "asset_description")
    merged["specific_bequests"] = dedup_by_key(merged["specific_bequests"], "beneficiary")

    # Deduplicate people
    merged["people"]["grantors"] = dedup_people_by_name(merged["people"]["grantors"])
    merged["people"]["children"] = dedup_people_by_name(merged["people"]["children"])
    merged["people"]["grandchildren"] = dedup_people_by_name(merged["people"]["grandchildren"])
    merged["people"]["initial_trustees"] = dedup_people_by_name(merged["people"]["initial_trustees"])
    merged["people"]["successor_trustees"] = dedup_people_by_name(merged["people"]["successor_trustees"])
    merged["people"]["other_beneficiaries"] = dedup_people_by_name(merged["people"]["other_beneficiaries"])

    # Fix successor trustee order
    for i, t in enumerate(merged["people"]["successor_trustees"]):
        t["order"] = i + 1

    # Combine validation notes
    merged["validation_notes"] = " | ".join(all_validation) if all_validation else ""

    # Generate unified summary via one small Bedrock call
    if all_summaries:
        facts_lines = []
        for key, value in merged.items():
            if key != "plain_english_summary":
                facts_lines.append(f"{key.replace('_', ' ').title()}: {value}")
        summaries = "\n".join(all_summaries[:3])
        facts_lines.append(f"Partial summaries:\n{summaries}")
        facts = "\n".join(facts_lines)
        merged["plain_english_summary"] = call_bedrock_summary(facts)

    return merged


# ─────────────────────────────────────────────
# ENSURE SCHEMA EXISTS
# ─────────────────────────────────────────────

def normalize_schema(data):
    data.setdefault("trust_name", "")
    data.setdefault("document_type", "")
    data.setdefault("governing_state", "")
    data.setdefault("date_executed", "")
    data.setdefault("plain_english_summary", "")
    data.setdefault("advisor_flags", [])
    data.setdefault("key_provisions", [])
    data.setdefault("validation_notes", "")

    if "people" not in data or not isinstance(data["people"], dict):
        data["people"] = {}

    p = data["people"]
    p.setdefault("grantors", [])
    p.setdefault("spouse", {})
    p.setdefault("children", [])
    p.setdefault("grandchildren", [])
    p.setdefault("initial_trustees", [])
    p.setdefault("successor_trustees", [])
    p.setdefault("other_beneficiaries", [])

    data.setdefault("trusts", [])
    data.setdefault("distribution_events", [])
    data.setdefault("specific_bequests", [])

    return data


# ─────────────────────────────────────────────
# NORMALIZE PEOPLE STRUCTURE
# ─────────────────────────────────────────────

def normalize_people(data):
    p = data.get("people", {})

    p["grantors"] = [
        {"name": g} if isinstance(g, str) else g
        for g in p.get("grantors", [])
    ]
    p["children"] = [
        {"name": c} if isinstance(c, str) else c
        for c in p.get("children", [])
    ]
    p["grandchildren"] = [
        {"name": gc} if isinstance(gc, str) else gc
        for gc in p.get("grandchildren", [])
    ]
    p["initial_trustees"] = [
        {"name": t, "type": "Individual"} if isinstance(t, str) else t
        for t in p.get("initial_trustees", [])
    ]

    normalized_successors = []
    for i, s in enumerate(p.get("successor_trustees", [])):
        if isinstance(s, str):
            normalized_successors.append({
                "name": s,
                "type": "Corporate" if "Trust" in s else "Individual",
                "order": i + 1
            })
        else:
            s.setdefault("order", i + 1)
            normalized_successors.append(s)
    p["successor_trustees"] = normalized_successors

    p["other_beneficiaries"] = [
        {"name": b} if isinstance(b, str) else b
        for b in p.get("other_beneficiaries", [])
    ]

    spouse = p.get("spouse")
    if isinstance(spouse, str):
        p["spouse"] = {"name": spouse}
    elif isinstance(spouse, dict):
        if "husband" in spouse:
            p["spouse"] = {"name": spouse["husband"]}
        elif "wife" in spouse:
            p["spouse"] = {"name": spouse["wife"]}
        elif "name" not in spouse:
            p["spouse"] = {}

    data["people"] = p
    return data


# ─────────────────────────────────────────────
# LAMBDA HANDLER
# ─────────────────────────────────────────────
def lambda_handler(event, context):

    http_method = (
        event.get("requestContext", {})
             .get("http", {})
             .get("method", "")
    )
    if http_method == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": ""
        }

    try:
        body = json.loads(event.get("body", "{}"))

        document_text = body.get("document_text")
        pdf_base64    = body.get("pdf_base64")
        prospect_info = body.get("prospect_info")

        if pdf_base64:
            document_text = extract_pdf_text(pdf_base64)
        elif document_text:
            document_text = sanitize_document_text(document_text)

        if not document_text or not document_text.strip():
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "No document text provided"})
            }

        # Build prospect context
        prospect_section = ""
        if prospect_info and isinstance(prospect_info, dict):
            lines = []
            if prospect_info.get("clientName"):
                lines.append(f"Client Name: {prospect_info['clientName']}")
            if prospect_info.get("maritalStatus"):
                lines.append(f"Marital Status: {prospect_info['maritalStatus']}")
            if prospect_info.get("dob"):
                lines.append(f"Date of Birth: {prospect_info['dob']}")
            if prospect_info.get("assets"):
                lines.append(f"Estimated Assets: {prospect_info['assets']}")
            if prospect_info.get("advisor"):
                lines.append(f"Current Advisor: {prospect_info['advisor']}")
            if prospect_info.get("notes"):
                lines.append(f"Advisor Notes: {prospect_info['notes']}")
            if lines:
                prospect_section = (
                    "\n\nPROSPECT CONTEXT (use to validate extracted names and flag discrepancies):\n"
                    + "\n".join(lines)
                )

        # Split into chunks
        chunks = split_into_chunks(document_text)
        print(f"Document: {len(document_text)} chars, {len(chunks)} chunk(s)")

        # Filter out boilerplate-only chunks before sending to Bedrock
        meaningful_chunks = []
        for i, chunk in enumerate(chunks):
            meaningful_words = len(re.findall(r'\b[a-zA-Z]{4,}\b', chunk))
            if meaningful_words < 20:
                print(f"Skipping chunk {i + 1} — too few meaningful words ({meaningful_words})")
            else:
                meaningful_chunks.append((i + 1, chunk))

        total = len(meaningful_chunks)
        print(f"Processing {total} meaningful chunks in parallel (max 5 workers)...")

        # Process chunks in parallel — Bedrock calls are I/O-bound so threads help significantly.
        # Cap at 5 workers to avoid overwhelming Bedrock's rate limits.
        chunk_results_map = {}

        def process_chunk(args):
            original_idx, chunk_num, chunk_text = args
            print(f"Analyzing chunk {chunk_num}/{total}...")
            result = analyze_chunk(chunk_text, chunk_num, total, prospect_section)
            return original_idx, result

        tasks = [(orig_i, seq_i + 1, chunk) for seq_i, (orig_i, chunk) in enumerate(meaningful_chunks)]

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(process_chunk, t): t for t in tasks}
            for future in as_completed(futures):
                try:
                    orig_idx, result = future.result()
                    chunk_results_map[orig_idx] = result
                except Exception as e:
                    print(f"Chunk failed: {e}")

        # Reassemble in original document order
        chunk_results = [chunk_results_map[orig_i] for orig_i, _ in meaningful_chunks if orig_i in chunk_results_map]

        if not chunk_results:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Document contained no extractable content after sanitization"})
            }

        # Python merge — no token limits
        print("Merging results in Python...")
        data = python_merge(chunk_results)
        data = normalize_schema(data)
        data = normalize_people(data)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(data)
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)})
        }
