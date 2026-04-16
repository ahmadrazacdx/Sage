import { v4 as uuidv4 } from "uuid";

// Mock Data
export const MOCK_SESSIONS = [
  { thread_id: "aaa00001", title: "Binary Search Trees", last_message_preview: "A BST is a tree where...", updated_at: new Date().toISOString() },
  { thread_id: "aaa00002", title: "OOP Concepts in Python", last_message_preview: "Classes define...", updated_at: new Date(Date.now() - 86400000).toISOString() },
  { thread_id: "aaa00003", title: "Database Normalization", last_message_preview: "1NF requires...", updated_at: new Date(Date.now() - 86400000 * 3).toISOString() },
];

export const MOCK_STATUS = {
  model_ready: true,
  model_name: "Qwen3.5-4B",
  embedding_tier: "lite",
  network_online: false,
  vectordb_collections: ["CS101", "CS201"],
  llm_port: 8080
};

export const MOCK_COURSES = { courses: ["CS101", "CS201", "CS202", "CS301", "CS302"] };

export const MOCK_DOCUMENTS = [
  { file: "syllabus.pdf", uploaded_at: new Date().toISOString(), chunks: 42 },
  { file: "lecture_1.pptx", uploaded_at: new Date(Date.now() - 86400000).toISOString(), chunks: 15 }
];

export const MOCK_MESSAGES: Record<string, any[]> = {
  "aaa00001": [
    { role: "user", content: "What is a Binary Search Tree?" },
    { role: "assistant", content: "A Binary Search Tree is a node-based binary tree data structure where each node has at most two child nodes." }
  ],
  "aaa00002": [
    { role: "user", content: "Explain classes in Python." },
    { role: "assistant", content: "Classes provide a means of bundling data and functionality together." }
  ]
};

// Simulated fetch override for development
export function initMocks() {
    // Always enable mocks in demo mode (VITE_USE_MOCK=true or not explicitly disabled)
  if (import.meta.env.VITE_USE_MOCK === 'false') return;
  console.log("🛠️ Sage Mock API Layer Initialized");

  const originalFetch = window.fetch;
  
  const jsonResponse = (data: unknown, status = 200) =>
    new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const method = init?.method?.toUpperCase() || 'GET';

    // Artificial delay to simulate network
    await new Promise(r => setTimeout(r, 200));

    if (url.includes('/api/status')) {
      return jsonResponse(MOCK_STATUS);
    }
    if (url.includes('/api/courses')) {
      return jsonResponse(MOCK_COURSES);
    }
    if (url.match(/\/api\/sessions\/[^/]+\/messages/) && method === 'GET') {
      const match = url.match(/\/api\/sessions\/([^/]+)\/messages/);
      const threadId = match?.[1] || '';
      return jsonResponse(MOCK_MESSAGES[threadId] || []);
    }
    if (url.includes('/api/sessions') && method === 'GET') {
      return jsonResponse(MOCK_SESSIONS);
    }
    if (url.includes('/api/documents') && method === 'GET') {
      return jsonResponse(MOCK_DOCUMENTS);
    }
    if (url.includes('/api/chat') && method === 'POST') {
      const body = JSON.parse(init?.body as string || '{}');
      if (MOCK_STATUS.model_ready === false) {
        return jsonResponse({ detail: 'Model not ready. Please wait.' }, 503);
      }
      const thread_id = body.thread_id || uuidv4();
      return jsonResponse({ thread_id, message_id: uuidv4() });
    }
    if (url.includes('/api/upload') && method === 'POST') {
      return jsonResponse({ status: 'ok', files_processed: 1, chunks_indexed: 10 });
    }
    if (method === 'DELETE') {
      return new Response(null, { status: 204 });
    }

    return originalFetch(input, init);
  };
}

type MockStreamEvent =
  | { type: "thinking"; text: string }
  | { type: "tool_call"; name: string }
  | { type: "chunk"; text: string }
  | { type: "error"; message: string }
  | { type: "done" };

export function mockStream(
  onChunk: (text: string) => void,
  onDone: () => void,
  onEvent?: (event: MockStreamEvent) => void,
) {
  const text = "Binary search is an efficient algorithm for finding an item in a **sorted** list.\n\n**Time Complexity:** $O(\\log n)$\n\nHere's an example:\n```python\ndef binary_search(arr, target):\n    lo, hi = 0, len(arr) - 1\n    while lo <= hi:\n        mid = lo + (hi - lo) // 2\n        if arr[mid] == target: return mid\n        elif arr[mid] < target: lo = mid + 1\n        else: hi = mid - 1\n    return -1\n```\n\n```mermaid\ngraph TD;\n    A[10] --> B[5];\n    A --> C[15];\n    B --> D[2];\n    B --> E[7];\n```";
  
  const tokens = text.split(' ');
  let i = 0;
  let cancelled = false;
  let stageOneTimeout: ReturnType<typeof setTimeout> | null = null;
  let stageTwoTimeout: ReturnType<typeof setTimeout> | null = null;
  let interval: ReturnType<typeof setInterval> | null = null;
  
  // Simulate thinking first
  onEvent?.({ type: "thinking", text: "Analyzing the request..." });
  
  stageOneTimeout = setTimeout(() => {
    if (cancelled) return;
    onEvent?.({ type: "tool_call", name: "corpus_search" });
    
    stageTwoTimeout = setTimeout(() => {
      if (cancelled) return;
      interval = setInterval(() => {
        if (cancelled) {
          if (interval) clearInterval(interval);
          return;
        }
        if (i < tokens.length) { 
          const chunk = tokens[i++] + " ";
          onChunk(chunk);
          onEvent?.({ type: "chunk", text: chunk });
        } else { 
          if (interval) clearInterval(interval);
          onEvent?.({ type: "done" });
          onDone(); 
        }
      }, 50);
    }, 1000);
  }, 500);

  return () => {
    cancelled = true;
    if (stageOneTimeout) clearTimeout(stageOneTimeout);
    if (stageTwoTimeout) clearTimeout(stageTwoTimeout);
    if (interval) clearInterval(interval);
  };
}
