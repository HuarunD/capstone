# Estate IQ Backend

Backend service for extracting structured information from estate planning documents using **AWS Lambda** and **Amazon Bedrock**.

This service accepts either raw document text or a base64-encoded PDF, extracts text when needed, **sanitizes boilerplate content**, splits the content into chunks, sends each chunk to an Anthropic model on Bedrock **in parallel**, merges chunk-level results in Python, normalizes the final schema, and returns a single JSON response.

---

## Overview

The backend supports an estate document analysis workflow for advisors and internal users. Its primary function is to transform unstructured estate planning documents into structured JSON that can be displayed in the frontend and used by downstream systems.

### Core capabilities
- Accept raw text or PDF input
- Extract text from PDFs
- Sanitize the document to strip signature blocks, notarial boilerplate, table of contents, and standard trustee-powers articles
- Split long documents into manageable chunks
- Skip chunks that are effectively boilerplate-only
- Call Amazon Bedrock for per-chunk structured extraction **concurrently (up to 5 workers)**
- Recover gracefully from truncated or malformed JSON output
- Merge and deduplicate chunk outputs in Python
- Normalize the final response schema
- Return structured JSON for downstream display or review

---

## Architecture

```text
User / Frontend
        ↓
API Gateway or Lambda Function URL
        ↓
AWS Lambda (Python)
        ├─ PDF text extraction (pypdf)
        ├─ Document sanitization (regex-based boilerplate strip)
        ├─ Chunking logic
        ├─ Meaningful-chunk filter
        ├─ Parallel Bedrock chunk analysis (ThreadPoolExecutor, max 5 workers)
        ├─ Hardened JSON parser with truncation recovery
        ├─ Python merge + deduplication
        └─ Schema + people normalization
        ↓
Amazon Bedrock
        └─ Anthropic Claude Haiku 4.5 model
```

### Processing flow
1. The client sends either `document_text` or `pdf_base64`.
2. If a PDF is provided, Lambda extracts text from the PDF; if raw text is provided, it is sanitized directly.
3. The sanitizer strips the table of contents, the generic trustee-powers article, electronic signatures, notarial acknowledgements, witness blocks, and other common boilerplate.
4. The document is split into chunks using character-based boundaries (preferring blank lines, then line breaks, then spaces).
5. Chunks that contain fewer than 20 meaningful words are skipped before any Bedrock call.
6. The remaining chunks are analyzed **in parallel** with Bedrock (up to 5 concurrent workers).
7. The backend parses each model response into JSON, recovering from markdown fences, trailing text, smart quotes, and token truncation when possible.
8. Chunk results are reassembled in original document order and merged in Python.
9. Duplicate entities and repeated provisions are removed.
10. The final schema and people structures are normalized so required fields always exist.
11. A structured JSON object is returned to the caller.

---

## Technology Stack

- **Python 3.12**
- **AWS Lambda**
- **Amazon Bedrock Runtime** via `boto3` (Converse API)
- **Anthropic Claude Haiku 4.5** model (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)
- **pypdf** for PDF parsing
- **botocore Config** for timeout and retry tuning
- **concurrent.futures.ThreadPoolExecutor** for parallel chunk analysis

---

## Main Configuration

### Bedrock client
The backend creates a Bedrock Runtime client in `us-east-1` with:

- `read_timeout = 300`
- `connect_timeout = 10`
- `retries.max_attempts = 2`

### Model
```python
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
```

### Chunk size
```python
CHUNK_SIZE = 3500
```

Chunks were reduced from 5000 to 3500 characters. Smaller chunks produce smaller JSON outputs per call, which keeps the model comfortably under its per-response token limit and reduces the frequency of truncated responses.

### Parallelism
```python
ThreadPoolExecutor(max_workers=5)
```

Bedrock calls are I/O-bound, so threads help significantly. The concurrency cap of 5 keeps the service well below typical Bedrock throttling limits while still giving a substantial speedup on multi-chunk documents.

### Inference config
- Chunk extraction: `maxTokens = 4096`, `temperature = 0`
- Summary generation: `maxTokens = 1000`, `temperature = 0`

---

## Request Format

The Lambda expects a JSON body.

### Supported input fields
- `document_text`: plain extracted text from a document
- `pdf_base64`: base64-encoded PDF string
- `prospect_info`: optional contextual information used to help validate extracted names and discrepancies

### Example request
```json
{
  "document_text": "THIS TRUST AGREEMENT is made on January 4, 2024...",
  "prospect_info": {
    "clientName": "Jane Doe",
    "maritalStatus": "Married",
    "dob": "1965-08-11",
    "assets": "$8M-$12M",
    "advisor": "Internal Advisory Team",
    "notes": "Client mentioned a revocable trust and two adult children."
  }
}
```

### PDF request example
```json
{
  "pdf_base64": "JVBERi0xLjcKJc...",
  "prospect_info": {
    "clientName": "John Doe"
  }
}
```

### Input precedence
If both `document_text` and `pdf_base64` are provided, the backend uses the PDF path and overwrites `document_text` with extracted PDF text. In either case, the resulting text is passed through the sanitizer before chunking.

---

## Response Format

The backend returns JSON in the response body.

### Success response
- HTTP `200`
- `Content-Type: application/json`
- body contains a normalized structured JSON object

### Error responses
- HTTP `400` when no usable text is provided
- HTTP `400` when the document contained no extractable content after sanitization
- HTTP `500` for unexpected failures

### Example success response
```json
{
  "trust_name": "Doe Family Revocable Trust",
  "document_type": "Revocable Trust",
  "governing_state": "California",
  "date_executed": "2024-01-04",
  "plain_english_summary": "This document creates a revocable trust for estate planning purposes...",
  "advisor_flags": [],
  "key_provisions": [],
  "people": {
    "grantors": [{ "name": "Jane Doe" }],
    "spouse": { "name": "John Doe" },
    "children": [],
    "grandchildren": [],
    "initial_trustees": [],
    "successor_trustees": [],
    "other_beneficiaries": []
  },
  "trusts": [],
  "distribution_events": [],
  "specific_bequests": [],
  "validation_notes": ""
}
```

### Example error response
```json
{
  "error": "No document text provided"
}
```

---

## Output Schema

The backend enforces the following top-level schema:

```json
{
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
    "other_beneficiaries": []
  },
  "trusts": [],
  "distribution_events": [],
  "specific_bequests": [],
  "validation_notes": ""
}
```

Even when data is missing, the backend normalizes the structure so these fields are present. Person entries are normalized so that the frontend always receives dictionary-shaped objects, and spouse aliases such as `husband` or `wife` are remapped to a `name` field.

---

## Bedrock Prompting Strategy

The service uses two prompt types:

### 1. Chunk extraction prompt
Used for each section of the document. The model is instructed to:

- return JSON only (no preamble, no markdown fences)
- keep all text values under 30 words
- produce evidence quotes 15–30 words long
- include a maximum of 5 items per array
- extract only information explicitly present in the section

### 2. Summary prompt
Used only after chunk processing when multiple partial summaries exist. It generates a three-sentence paragraph followed by a bulleted list covering party, amount, and distribution trigger in post-death order. The model is told to use only explicit facts and to preserve names and numbers exactly.

---

## Core Functions

### `sanitize_document_text(text)`
Regex-based cleanup of common boilerplate:

- Strips a table of contents when it precedes the first real Article One.
- Removes the generic trustee-powers article (Articles Eleven/Twelve) while preserving any signature block that follows it.
- Strips electronic `/s/` signature lines, notarial acknowledgement blocks, `COUNTY OF ...` lines, commission-expiration notices, witness markers, `(Seal)` markers, and isolated single-digit notarial artifacts.
- Removes `THIS INSTRUMENT PREPARED BY` and `I Affirm, under the penalties for perjury` blocks.
- Collapses runs of blank lines.

### `extract_pdf_text(pdf_base64)`
Decodes a base64 PDF, loads it with `PdfReader`, extracts text page by page, and annotates each page with `[Page N]` markers. The assembled text is then passed through `sanitize_document_text`.

### `split_into_chunks(text, chunk_size=CHUNK_SIZE)`
Splits long text into chunks of roughly 3500 characters. It tries to break on:

1. blank lines
2. line breaks
3. spaces
4. hard cutoff if no cleaner break exists

### `call_bedrock(prompt)`
Sends a chunk extraction request to Bedrock using the main system prompt.

### `call_bedrock_summary(facts)`
Sends a lightweight summary request to Bedrock using the summary-only prompt.

### `_repair_json(text)`
Lightweight repair pass that replaces smart quotes with ASCII equivalents and flattens unescaped newlines that appear inside string values.

### `parse_json(text)`
Five-step parser:

1. Strip markdown fences if present.
2. Try a direct `json.loads`.
3. Extract the outermost `{...}` block (first `{` through last `}`) and try again.
4. Apply `_repair_json` to the extracted candidate and try again.
5. **Truncation recovery**: if the model ran out of tokens mid-output, walk backwards from the end, close any open strings/brackets/braces, and return the best partial result.

### `analyze_chunk(chunk_text, chunk_num, total_chunks, prospect_section="")`
Builds the per-chunk analysis prompt, sends it to Bedrock, and parses the result.

### `dedup_by_key(items, key)`
Deduplicates lists of dictionaries based on a selected field.

### `dedup_people_by_name(items)`
Deduplicates person-like objects based on lowercase normalized names.

### `python_merge(chunk_results)`
Combines all chunk-level results into one final structure. This function:

- selects the first non-empty scalar values
- merges arrays from all chunks
- merges people entities
- deduplicates repeated entries
- reassigns successor trustee order
- combines validation notes
- generates a unified summary via a single small Bedrock call when multiple partial summaries exist

### `normalize_schema(data)`
Ensures that required keys always exist in the final response.

### `normalize_people(data)`
Normalizes person structures so the frontend receives consistent objects. It converts strings into structured dictionaries, infers trustee type from the name (`"Trust"` substring → corporate), and handles spouse aliases such as `husband` or `wife`.

### `lambda_handler(event, context)`
Main Lambda entry point. Handles:

- CORS preflight `OPTIONS`
- request body parsing
- input validation
- PDF extraction or raw-text sanitization
- optional prospect context construction
- boilerplate-only chunk filtering
- **parallel chunk analysis via ThreadPoolExecutor**
- reassembly in original document order
- Python merge and normalization
- HTTP response generation

---

## Prospect Context Support

The backend can optionally add external prospect metadata into the chunk prompt.

Supported fields:

- `clientName`
- `maritalStatus`
- `dob`
- `assets`
- `advisor`
- `notes`

This context is not treated as extracted document content. Instead, it is appended as validation context to help the model compare names, detect discrepancies, and interpret references more consistently.

---

## CORS / HTTP Behavior

The handler checks the incoming HTTP method from:

```python
event.get("requestContext", {}).get("http", {}).get("method", "")
```

If the method is `OPTIONS`, it returns:

- status `200`
- `Content-Type: application/json`
- empty body

This supports browser-based requests from the frontend.

---

## Error Handling

### 400 Bad Request
Returned when no usable text is available after request parsing, or when no chunk survived the meaningful-content filter.

Examples:
```json
{"error": "No document text provided"}
```
```json
{"error": "Document contained no extractable content after sanitization"}
```

### 500 Internal Server Error
Returned for unexpected runtime failures, including PDF parsing, Bedrock failures, or JSON parsing failures that could not be recovered.

```json
{"error": "<exception message>"}
```

The service also logs processing steps such as:

- document size and chunk count
- chunks skipped as boilerplate
- chunk being analyzed
- truncation-recovery warnings
- merge progress
- exception messages

---

## Deployment Notes

This backend is intended for AWS Lambda deployment.

### Runtime
- Python 3.12 (pypdf bytecode in the deployment bundle is compiled for `cpython-312`).

### Packaging
The deployment bundle (the provided `.zip`) includes the handler and its runtime dependencies at the top level:

```
lambda_function.py
pypdf/
pypdf-6.8.0.dist-info/
```

`boto3` and `botocore` do not need to be bundled — they are available in the Lambda Python runtime — but `pypdf` must be. If you rebuild the bundle, pip-install `pypdf` into the project root (or into a Lambda layer) before zipping.

### AWS permissions needed
At minimum, the execution role should be able to:

- write logs to CloudWatch
- invoke Bedrock Runtime (`bedrock:InvokeModel` / `bedrock:Converse` on the target model ARN)

Depending on deployment style, it may also need permissions for API Gateway or Lambda URL integration.

### Recommended Lambda settings
- Memory: 1024 MB or higher (PDF parsing is memory-hungry for large files).
- Timeout: 300 seconds (matches the Bedrock `read_timeout`).
- Architecture: `arm64` is fine since pypdf is pure Python.

---

## Dependencies

Based on the code, the backend requires:

```txt
boto3
botocore
pypdf
```

`boto3` and `botocore` are already present in the Lambda Python runtime; only `pypdf` needs to be bundled for deployment. For local development, install all three.

---

## Local Development Notes

To run similar logic locally, you need:

- Python 3.12
- AWS credentials configured with Bedrock access
- model access to `us.anthropic.claude-haiku-4-5-20251001-v1:0` in `us-east-1`

### Install
```bash
pip install -r requirements.txt
```

### Invoke locally
```python
import json
from lambda_function import lambda_handler

event = {
    "requestContext": {"http": {"method": "POST"}},
    "body": json.dumps({
        "document_text": open("sample_trust.txt").read()
    })
}
print(lambda_handler(event, None))
```

---

## Example Lambda Event Body

```json
{
  "body": "{\"document_text\": \"This Trust Agreement...\", \"prospect_info\": {\"clientName\": \"Jane Doe\"}}",
  "requestContext": {
    "http": {
      "method": "POST"
    }
  }
}
```

---

## Design Decisions

### Why sanitize before chunking?
Signature blocks, notary acknowledgements, and generic trustee-powers articles account for a surprisingly large share of document text and add no extractable value. They also frequently cause the model to emit malformed JSON (for example, the sequence `) SS:` appearing inside a quoted evidence string). Stripping them up-front shortens the document, reduces token cost, and improves JSON stability.

### Why chunk the document?
Estate planning documents can be long. Chunking reduces prompt size, improves reliability, and avoids exceeding model context or output limits.

### Why process chunks in parallel?
Bedrock calls are network-bound. A thread pool of five workers reduces wall-clock time roughly linearly with chunk count while staying below typical Bedrock rate limits.

### Why merge in Python instead of Bedrock?
The backend merges chunk outputs in Python to avoid extra token cost, reduce latency, and maintain deterministic post-processing.

### Why normalize the final schema?
A consistent response structure simplifies frontend rendering and reduces UI errors caused by missing keys or inconsistent person formats.

### Why aggressive JSON recovery?
In production we regularly see the model hit its 4096-token ceiling mid-object. Rather than fail the whole chunk, the parser closes the structure and returns the partial result so that the rest of the document still contributes to the merge.

---

## Known Limitations

- PDF extraction quality depends on whether the PDF contains machine-readable text.
- Scanned PDFs without readable text may fail or return poor results unless OCR is added elsewhere.
- Deduplication is heuristic and may collapse distinct entries with identical keys.
- `distribution_events` are deduplicated by `asset_description`, which may be too aggressive for some documents.
- `specific_bequests` are deduplicated by `beneficiary`, which may merge multiple bequests to the same beneficiary.
- The sanitizer's trustee-powers stripping targets English-language articles titled "Eleven" or "Twelve"; documents that use different numbering will not benefit from this optimization.
- Truncation recovery produces a partial JSON object; some array items from that chunk may be missing.

---

## Repository Role

This backend is responsible for the document-analysis engine of the Estate IQ system. The frontend handles intake, document upload, and result rendering, while this backend performs:

- text extraction
- document sanitization
- AI-based information extraction (parallelized)
- post-processing and merging
- normalized JSON response generation

---

## Summary

Estate IQ Backend is a serverless AWS Lambda service that transforms estate planning documents into structured advisor-friendly JSON. It combines deterministic Python preprocessing (sanitization, chunking, boilerplate filtering), parallel Bedrock-based extraction, hardened JSON parsing with truncation recovery, and deterministic post-processing to produce consistent, renderable output for a frontend application.
