# API Reference

This document describes all REST and SSE endpoints exposed by the Sage backend.
The API server runs on `http://localhost:8765` by default.

---

## Table of Contents

- [System Endpoints](#system-endpoints)
- [Chat Endpoints](#chat-endpoints)
- [Session Endpoints](#session-endpoints)
- [Document Endpoints](#document-endpoints)
- [Artifact Endpoints](#artifact-endpoints)
- [SSE Event Types](#sse-event-types)

---

## System Endpoints

### GET /api/healthz

Liveness probe. Always returns 200.

#### Response

```json
{
  "status": "ok"
}
```

### GET /api/status

System health snapshot. Polled by the frontend until `model_ready` is `true`.

#### Response

```json
{
  "model_ready": true,
  "model_name": "Qwen3.5-2B",
  "llm_port": 8080,
  "embedding_model": "bge-small-en-v1.5",
  "vectordb_collections": ["curriculum"],
  "network_online": false
}
```

| Field | Type | Description |
| --- | --- | --- |
| `model_ready` | boolean | Whether the LLM server has completed loading |
| `model_name` | string | Name of the active primary model |
| `llm_port` | integer or null | Port of the primary LLM server |
| `embedding_model` | string | Name of the active embedding model |
| `vectordb_collections` | array of string | Available ChromaDB collections |
| `network_online` | boolean | Whether internet connectivity is detected |

### GET /api/courses

List available course codes.

#### Response

```json
{
  "courses": ["CS101", "CS201"]
}
```

## Chat Endpoints

### POST /api/chat

Submit a user message for processing. This creates a pending stream that must
be consumed via the SSE endpoint.

#### Response

```json
{
  "thread_id": "abc123",
  "message": "Explain binary search",
  "mode": "explain",
  "course": "all"
}
```

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `thread_id` | string or null | No | Conversation thread identifier. Generated if omitted. |
| `message` | string | Yes | User message (1 to 2500 characters) |
| `mode` | string | Yes | Agent mode (see valid modes below) |
| `course` | string | No | Course filter. Default: `"all"` |

**Valid Modes**

`explain`, `thinking`, `general`, `quiz`, `diagram`, `roadmap`, `research`, `fix`

**Response** (200)

```json
{
  "thread_id": "abc123",
  "message_id": "d4e5f6a7"
}
```

**Error Responses**

| Status | Condition |
| --- | --- |
| 503 | Model not ready |
| 422 | Invalid mode or message validation failure |
| 409 | Pending or active stream already exists for this thread |

### GET /api/stream/{thread_id}

Server-Sent Events stream for receiving agent responses. Must be called after
a successful `POST /api/chat` for the same `thread_id`.

**Response**: `text/event-stream`

Each event is a JSON object on a `data:` line. See
[SSE Event Types](#sse-event-types) for the full event schema.

**Error Responses**

| Status | Condition |
| --- | --- |
| 404 | No pending stream for this thread |
| 409 | Stream already active for this thread |

## Session Endpoints

### GET /api/sessions

List all conversation sessions, newest first.

#### Response

```json
[
  {
    "thread_id": "abc123",
    "title": "Binary Search Explanation",
    "last_message_preview": "Binary search works by...",
    "updated_at": "2026-01-15T10:30:00Z"
  }
]
```

### GET /api/sessions/{thread_id}/messages

Retrieve the full message history for a conversation.

#### Response

```json
[
  {
    "role": "user",
    "content": "Explain binary search",
    "artifact": null
  },
  {
    "role": "assistant",
    "content": "Binary search is a divide-and-conquer algorithm...",
    "artifact": {
      "kind": "pdf",
      "filename": "research_binary_search.pdf",
      "path": "/exports/research_binary_search.pdf",
      "url": "/api/artifacts/research_binary_search.pdf"
    }
  }
]
```

### DELETE /api/sessions/{thread_id}

Delete a conversation and all associated data (metadata, checkpointer state,
in-memory cache).

**Response**: 204 No Content

## Document Endpoints

### POST /api/upload

Upload one or more documents for RAG indexing.

**Request**: `multipart/form-data`

| Field | Type | Description |
| --- | --- | --- |
| `files` | file[] | One or more document files |
| `course` | string | Course code for metadata. Default: `"all"` |

#### Response

```json
{
  "status": "ok",
  "files_processed": 2,
  "chunks_indexed": 0
}
```

**Error Responses**

| Status | Condition |
| --- | --- |
| 400 | Unsupported file type |
| 413 | Upload limit exceeded |

### GET /api/documents

List all user-uploaded documents.

**Response**

```json
[
  {
    "file": "lecture_notes.pdf",
    "uploaded_at": "2026-01-15T10:30:00Z",
    "chunks": 0
  }
]
```

### DELETE /api/documents/{filename}

Remove an uploaded document by filename.

**Response**: 204 No Content

**Error**: 404 if the document is not found.

## Artifact Endpoints

### GET /api/artifacts

List all generated export files (PDF, SVG, Markdown, plain text), newest first.

#### Response

```json
[
  {
    "kind": "pdf",
    "filename": "research_transformers.pdf",
    "size_bytes": 245120,
    "created_at": "2026-01-15T10:30:00Z",
    "date_label": "Wednesday, January 15, 2026",
    "url": "/api/artifacts/research_transformers.pdf"
  }
]
```

### GET /api/artifacts/{filename}

Download a generated artifact file.

**Response**: File download with appropriate MIME type.

| Extension | MIME Type |
| --- | --- |
| `.pdf` | `application/pdf` |
| `.svg` | `image/svg+xml` |
| `.md` | `text/markdown` |
| `.txt` | `text/plain` |

**Error**: 404 if the artifact is not found.

## SSE Event Types

All events emitted on the `GET /api/stream/{thread_id}` endpoint follow the
format:

```text
data: {"type": "<event_type>", ...fields}
```

### Event Schema

| Type | Fields | Description |
| --- | --- | --- |
| `node_start` | `node`, `label` | An agent graph node has begun execution |
| `tool_call` | `name`, `raw_name`, `label` | A tool invocation has started |
| `chunk` | `text` | A fragment of the response text |
| `thinking` | `text` | A fragment of the model's thought process (thinking mode) |
| `artifact` | `kind`, `filename`, `path`, `url` | A downloadable artifact has been generated |
| `heartbeat` | (none) | Connection keep-alive signal (every 10 seconds) |
| `done` | (none) | Stream has completed successfully |
| `error` | `message` | An error occurred during processing |

### Event Flow

A typical SSE session follows this sequence:

```text
node_start (router)
node_start (retrieval)
tool_call (corpus_search)     -- if RAG retrieval is performed
node_start (reasoning)
chunk (text fragment)         -- repeated, streaming response
chunk (text fragment)
done
```

For batch intents (quiz, diagram, roadmap, research, explain, fix), token-level
streaming is suppressed. The response is emitted as a single chunk after graph
completion:

```text
node_start (router)
node_start (retrieval)
node_start (quiz)
chunk (full response)
done
```

### Node Labels

| Node | Label |
| --- | --- |
| `retrieval` | Retrieving course materials |
| `general` | Generating answer |
| `reasoning` | Reasoning through content |
| `response_generator` | Formatting response |
| `quiz` | Generating quiz |
| `diagram` | Building diagram |
| `planner` | Building study plan |
| `research` | Researching topic |
| `code_fix` | Analysing code |

### Tool Labels

| Tool | Label |
| --- | --- |
| `validate_mermaid` | Validating diagram syntax |
| `render_mermaid_svg` | Rendering diagram |
| `search_arxiv` | Searching arXiv |
| `search_web` | Searching the web |
| `search_wikipedia` | Searching Wikipedia |
| `calculator` | Running calculation |
| `execute_python` | Executing code |
| `export_pdf` | Generating PDF report |
| `export_markdown` | Saving markdown |
| `corpus_search` | Searching course materials |
