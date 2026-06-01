# API Reference

> REST and SSE endpoint reference for the Sage backend. Default: `http://localhost:8765`

## System Endpoints

### `GET /api/healthz`

Liveness probe. Returns 200 unconditionally.

```json
{ "status": "ok" }
```

### `GET /api/status`

System health snapshot. Frontend polls until `model_ready` is `true`.

| Field | Type | Description |
| --- | --- | --- |
| `model_ready` | `boolean` | Primary LLM server loaded |
| `model_name` | `string` | Active primary model identifier |
| `llm_port` | `integer \| null` | Primary LLM server TCP port |
| `embedding_model` | `string` | Active embedding model |
| `vectordb_collections` | `string[]` | Available ChromaDB collections |
| `network_online` | `boolean` | Internet connectivity detected |

### `GET /api/courses`

Returns available course codes from ingested curriculum.

```json
{ "courses": ["CS101", "CS201"] }
```

## Chat Endpoints

### `POST /api/chat`

Submits a user message and creates a pending SSE stream.

#### Request Body

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `thread_id` | `string \| null` | No | Thread identifier. Auto-generated if omitted. |
| `message` | `string` | Yes | User message (1–2200 chars) |
| `mode` | `string` | Yes | One of: `explain`, `thinking`, `general`, `quiz`, `diagram`, `roadmap`, `research`, `fix` |
| `course` | `string` | No | Course filter. Default: `"all"` |

#### Response (200)

```json
{ "thread_id": "abc123", "message_id": "d4e5f6a7" }
```

| Status | Condition |
| --- | --- |
| `503` | Model not ready |
| `422` | Invalid mode or message validation failure |
| `409` | Pending or active stream already exists for this thread |

### `GET /api/stream/{thread_id}`

SSE stream for agent responses. Must follow a successful `POST /api/chat`.

**Response**: `text/event-stream`, see [SSE Event Types](#sse-event-types).

| Status | Condition |
| --- | --- |
| `404` | No pending stream for this thread |
| `409` | Stream already active for this thread |

## Session Endpoints

### `GET /api/sessions`

Lists all conversations, most recent first.

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

### `GET /api/sessions/{thread_id}/messages`

Full message history for a conversation.

```json
[
  { "role": "user", "content": "Explain binary search", "artifact": null },
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

### `DELETE /api/sessions/{thread_id}`

Deletes a conversation and all associated data. **Response**: `204 No Content`

## Artifact Endpoints

### `GET /api/artifacts`

Lists generated export files (PDF, SVG, Markdown, text), most recent first.

### `GET /api/artifacts/{filename}`

Downloads a generated artifact.

| Extension | MIME Type |
| --- | --- |
| `.pdf` | `application/pdf` |
| `.svg` | `image/svg+xml` |
| `.md` | `text/markdown` |
| `.txt` | `text/plain` |

**Error**: `404` if the artifact does not exist.

## SSE Event Types

All events on `GET /api/stream/{thread_id}`:

```text
data: {"type": "<event_type>", ...fields}
```

### Event Schema

| Type | Fields | Description |
| --- | --- | --- |
| `node_start` | `node`, `label` | Agent graph node began execution |
| `tool_call` | `name`, `raw_name`, `label` | Tool invocation started |
| `chunk` | `text` | Response text fragment |
| `thinking` | `text` | Model reasoning trace (thinking mode) |
| `artifact` | `kind`, `filename`, `path`, `url` | Downloadable artifact generated |
| `heartbeat` | — | Keep-alive (every 10s) |
| `done` | — | Stream completed |
| `error` | `message` | Error occurred |

### Event Flow: Streaming (`general`, `thinking`)

```text
node_start  (retrieval)
tool_call   (corpus_search)
node_start  (general)
chunk       (token) ← repeated
done
```

### Event Flow: Batch (`explain`, `quiz`, `diagram`, `roadmap`, `research`, `fix`)

```text
node_start  (retrieval)
node_start  (quiz)
chunk       (full response)
done
```

### Event Flow: Research with Artifact

```text
node_start  (research)
tool_call   (search_arxiv)
tool_call   (search_web)
tool_call   (export_pdf)
chunk       (summary)
artifact    (pdf)
done
```
