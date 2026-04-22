# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Contains the Sage AI Study Assistant frontend — a React SPA designed to connect to a Python FastAPI backend (Sage AI study assistant for Thal University Bhakkar).

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5 (api-server)
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)
- **Frontend**: React 18 + Vite + Tailwind CSS

## Structure

```text
artifacts-monorepo/
├── artifacts/
│   ├── api-server/         # Express API server
│   └── sage/               # Sage AI Study Assistant frontend (React + Vite)
├── lib/                    # Shared libraries
│   ├── api-spec/           # OpenAPI spec + Orval codegen config (Sage API)
│   ├── api-client-react/   # Generated React Query hooks
│   ├── api-zod/            # Generated Zod schemas from OpenAPI
│   └── db/                 # Drizzle ORM schema + DB connection
├── scripts/                # Utility scripts
├── pnpm-workspace.yaml
├── tsconfig.base.json
├── tsconfig.json
└── package.json
```

## Sage Frontend (artifacts/sage)

The Sage AI Study Assistant is a feature-complete React SPA:

### Features

- **8 AI modes**: General, Explain, Quiz Me, Diagram, Study Plan, Research, Fix Code, Thinking
- **Full SSE streaming**: Real-time token-by-token response with typing cursor animation
- **Mermaid diagrams**: Auto-detected and rendered from assistant responses
- **Markdown rendering**: Full GFM support with syntax-highlighted code blocks
- **Conversation history**: Grouped by Today/Yesterday/This Week/Last Month
- **Document management**: Upload (.pdf, .docx, .pptx, .md, .txt) / delete files
- **Course filter**: Dynamic course selector from API
- **Mock mode**: Active by default for demo (VITE_USE_MOCK=false to disable)
- **Collapsible sidebar**: State persisted in localStorage
- **First-run onboarding**: Name capture screen, stored in localStorage
- **Model loading screen**: Full-screen overlay while model initializes

### API Contract

The frontend connects to a Python FastAPI backend at the same origin (port 8765 in production):

- `POST /api/chat` - Submit message, get thread_id + message_id
- `GET /api/stream/{thread_id}` - SSE stream for response
- `GET /api/sessions` - List conversation sessions
- `DELETE /api/sessions/{thread_id}` - Delete session
- `GET /api/sessions/{thread_id}/messages` - Load conversation
- `POST /api/upload` - Upload documents (multipart)
- `GET /api/documents` - List uploaded documents
- `DELETE /api/documents/{filename}` - Delete document
- `GET /api/courses` - Available course codes
- `GET /api/status` - System health + model readiness

### Mock Mode

Set `VITE_USE_MOCK=false` to disable mock and connect to real backend.
Default behavior: mock API is always enabled for demo purposes.

### Colors (exact spec)

- Background: #212121
- Sidebar: #171717
- Sidebar border: #242424
- User bubble: #2b3445
- Input area bg: #2a2a2a / border: #3a3a3a
- Text primary: #e0e0e0
- Text muted: #5e5e5e

### Key Files

- `artifacts/sage/src/pages/Home.tsx` - Main chat page
- `artifacts/sage/src/components/Sidebar.tsx` - Sidebar with sessions + docs
- `artifacts/sage/src/components/Composer.tsx` - Message input bar
- `artifacts/sage/src/components/Markdown.tsx` - Markdown + Mermaid renderer
- `artifacts/sage/src/hooks/use-chat-stream.ts` - SSE streaming hook
- `artifacts/sage/src/api/mock.ts` - Mock API layer
- `artifacts/sage/src/lib/utils.ts` - Sage modes + tool names
- `artifacts/sage/src/index.css` - Dark theme CSS variables

## TypeScript & Composite Projects

Every package extends `tsconfig.base.json` which sets `composite: true`. The root `tsconfig.json` lists all packages as project references.

- **Always typecheck from the root** — run `pnpm run typecheck`
- **`emitDeclarationOnly`** — we only emit `.d.ts` files during typecheck
- **Project references** — when package A depends on package B, A's `tsconfig.json` must list B in its `references` array

## Root Scripts

- `pnpm run build` — runs `typecheck` first, then recursively runs `build` in all packages
- `pnpm run typecheck` — runs `tsc --build --emitDeclarationOnly` using project references

## Codegen

Run codegen: `pnpm --filter @workspace/api-spec run codegen`

This generates:

1. `lib/api-client-react/src/generated/` — React Query hooks + fetch client
2. `lib/api-zod/src/generated/` — Zod schemas
