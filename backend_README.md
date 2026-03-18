# Estate IQ Backend

Backend service for extracting structured information from estate planning documents using **AWS Lambda** and **Amazon Bedrock**.

This service accepts either raw document text or a base64-encoded PDF, extracts text when needed, splits the content into chunks, sends each chunk to an Anthropic model on Bedrock for structured extraction, merges chunk-level results in Python, normalizes the final schema, and returns a single JSON response.

---

## Overview

The backend is designed to support an estate document analysis workflow for advisors or internal users. Its main job is to convert unstructured estate planning documents into structured JSON that can be rendered in a frontend application.

### Core capabilities
- Accept raw text or PDF input
- Extract text from PDFs
- Split long documents into manageable chunks
- Call Amazon Bedrock for per-chunk structured extraction
- Merge and deduplicate chunk outputs in Python
- Normalize the final response schema
- Return structured JSON for downstream display or review

---

## Architecture

```text
Client / Frontend
        ↓
API Gateway or Lambda Function URL
        ↓
AWS Lambda (Python)
        ├─ PDF text extraction (pypdf)
        ├─ Chunking logic
        ├─ Bedrock chunk analysis calls
        ├─ Python merge + deduplication
        └─ Schema normalization
        ↓
Amazon Bedrock
        └─ Anthropic Claude Haiku model
```

### Processing flow
1. The client sends either `document_text` or `pdf_base64`.
2. If a PDF is provided, Lambda extracts text from the PDF.
3. The full document is split into chunks using character-based boundaries.
4. Each chunk is analyzed independently with Bedrock.
5. The backend parses each model response into JSON.
6. Chunk results are merged in Python.
7. Duplicate entities and repeated provisions are removed.
8. The final schema is normalized so required fields always exist.
9. A structured JSON object is returned to the caller.

---

## Technology Stack

- **Python**
- **AWS Lambda**
- **Amazon Bedrock Runtime** via `boto3`
- **Anthropic Claude Haiku** model
- **pypdf** for PDF parsing
- **botocore Config** for timeout and retry tuning

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
CHUNK_SIZE = 5000
```

This chunk size is used to split long extracted text before sending it to Bedrock.

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
If both `document_text` and `pdf_base64` are provided, the backend uses the PDF path and overwrites `document_text` with extracted PDF text.

---

## Response Format

The backend returns JSON in the response body.

### Success response
- HTTP `200`
- `Content-Type: application/json`
- body contains a normalized structured JSON object

### Error responses
- HTTP `400` when no usable text is provided
- HTTP `500` for unexpected failures

### Example success response
```json
{
  "trust_name": "Doe Family Revocable Trust",
  "document_type": "Revocable Trust",
  "governing_state": "California",
  "date_executed": "2024-01-04",
  "plain_english_summary": "This document appears to create a revocable trust for estate planning purposes...",
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

Even when data is missing, the backend normalizes the structure so these fields are present.

---

## Bedrock Prompting Strategy

The service uses two prompt types:

### 1. Chunk extraction prompt
Used for each section of the document. The model is instructed to:
- return JSON only
- extract only explicitly stated information
- keep text fields short
- include evidence quotes and locations
- limit array sizes

### 2. Summary prompt
Used only after chunk processing when multiple partial summaries exist. It generates one plain-English summary paragraph based on merged facts.

---

## Core Functions

### `extract_pdf_text(pdf_base64)`
Decodes a base64 PDF, loads it with `PdfReader`, extracts text page by page, and annotates each page with `[Page N]` markers.

### `split_into_chunks(text, chunk_size=CHUNK_SIZE)`
Splits long text into chunks of roughly 5000 characters. It tries to break on:
1. blank lines
2. line breaks
3. spaces
4. hard cutoff if no cleaner break exists

### `call_bedrock(prompt)`
Sends a chunk extraction request to Bedrock using the main system prompt.

### `call_bedrock_summary(facts)`
Sends a lightweight summary request to Bedrock using the summary-only prompt.

### `parse_json(text)`
Attempts to safely parse model output into JSON. It strips code fences if present and falls back to extracting the outermost JSON object when needed.

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
- optionally generates a unified summary

### `normalize_schema(data)`
Ensures that required keys always exist in the final response.

### `normalize_people(data)`
Normalizes person structures so the frontend receives consistent objects. It converts strings into structured dictionaries where necessary and handles spouse aliases such as `husband` or `wife`.

### `lambda_handler(event, context)`
Main Lambda entry point. Handles:
- CORS preflight `OPTIONS`
- request body parsing
- input validation
- PDF extraction
- optional prospect context construction
- chunk analysis loop
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

This context is not treated as extracted document content. Instead, it is appended as validation context to help the model:
- compare names
- detect discrepancies
- interpret references more consistently

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
Returned when no usable text is available after request parsing.

Example:
```json
{"error": "No document text provided"}
```

### 500 Internal Server Error
Returned for unexpected runtime failures, including PDF parsing, Bedrock failures, or JSON parsing failures.

Example:
```json
{"error": "<exception message>"}
```

The service also logs processing steps such as:
- document size
- chunk count
- current chunk being analyzed
- merge progress
- exception messages

---

## Deployment Notes

This backend is intended for AWS Lambda deployment.

### Likely deployment pattern
- Python Lambda function
- exposed through API Gateway or Lambda Function URL
- IAM permissions allowing Bedrock Runtime invocation
- packaged with third-party dependencies such as `pypdf`

### AWS permissions needed
At minimum, the execution role should be able to:
- write logs to CloudWatch
- invoke Bedrock Runtime

Depending on deployment style, it may also need permissions for related API Gateway or Lambda URL integration.

---

## Dependencies

Based on the code, the backend requires at least:

```txt
boto3
botocore
pypdf
```

If you package this as a Lambda deployment artifact, make sure these dependencies are included in the deployment bundle or Lambda layer.

---

## Local Development Notes

To run similar logic locally, you would need:
- Python 3.x
- AWS credentials configured with Bedrock access
- access to the target Bedrock model in `us-east-1`

### Example install
```bash
pip install boto3 botocore pypdf
```

The current code is Lambda-oriented, so local execution would typically require a small wrapper or a test event payload.

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

### Why chunk the document?
Estate planning documents can be long. Chunking reduces prompt size, improves reliability, and avoids exceeding model context or output limits.

### Why merge in Python instead of Bedrock?
The backend merges chunk outputs in Python to avoid extra token cost, reduce latency, and maintain deterministic post-processing.

### Why normalize the final schema?
A consistent response structure simplifies frontend rendering and reduces UI errors caused by missing keys or inconsistent person formats.

---

## Known Limitations

- PDF extraction quality depends on whether the PDF contains machine-readable text.
- Scanned PDFs without readable text may fail or return poor results unless OCR is added elsewhere.
- Deduplication is heuristic and may collapse distinct entries with identical keys.
- `distribution_events` are deduplicated by `asset_description`, which may be too aggressive for some documents.
- `specific_bequests` are deduplicated by `beneficiary`, which may merge multiple bequests to the same beneficiary.
- The service assumes Bedrock returns reasonably well-formed JSON.
- Long documents may result in multiple Bedrock calls and increased latency.

---

## Possible Improvements

- Add OCR support for scanned PDFs
- Move model ID and chunk size into environment variables
- Add stronger validation with JSON Schema or Pydantic
- Add structured logging and request tracing
- Add metrics for chunk counts, latency, and parse failures
- Add authentication and authorization if exposed publicly
- Add unit tests for merge, normalization, and deduplication logic
- Return explicit CORS headers if required by the deployment environment

---

## Repository Role

This backend is responsible for the document-analysis engine of the Estate IQ system. The frontend handles intake, document upload, and result rendering, while this backend performs:
- text extraction
- AI-based information extraction
- post-processing
- normalized JSON response generation

---

## Summary

Estate IQ Backend is a serverless AWS Lambda service that transforms estate planning documents into structured advisor-friendly JSON. It combines deterministic Python preprocessing and post-processing with Bedrock-based extraction to produce consistent, renderable output for a frontend application.
