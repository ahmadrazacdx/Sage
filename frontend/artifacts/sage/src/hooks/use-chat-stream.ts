import { useState, useCallback, useRef } from "react";
import { IS_MOCK_ENABLED, mockStream } from "@/api/mock";
import { useQueryClient } from "@tanstack/react-query";
import { getListSessionsQueryKey } from "@workspace/api-client-react";

const TIMELINE_MODES: ReadonlySet<string> = new Set(["quiz", "roadmap", "explain", "research", "diagram", "fix"]);

const RESEARCH_SEARCH_TOOLS: ReadonlySet<string> = new Set([
  "search_arxiv",
  "search_web",
  "search_wikipedia",
]);

function stripDecorativePrefix(label: string): string {
  return label.replace(/^[^A-Za-z0-9]+\s*/, "").trim();
}

function normalizeNodeStepLabel(mode: string | null, node?: string, fallbackLabel?: string): string | null {
  const nodeName = (node || "").trim();

  // Hide retrieval line for specific modes as requested
  if (nodeName === "retrieval" && (mode === "diagram" || mode === "fix")) {
    return null;
  }

  if (mode === "quiz") {
    if (nodeName === "retrieval") return "📚 Retrieving context…";
    if (nodeName === "quiz") return "🧩 Generating quiz questions…";
  }
  if (mode === "roadmap") {
    if (nodeName === "planner") return "📅 Building study plan…";
  }
  if (mode === "explain") {
    if (nodeName === "reasoning") return "🧠 Reasoning through answer…";
  }

  if (nodeName === "retrieval") return "📚 Retrieving context…";
  if (nodeName === "response_generator") return "✍️ Formatting response…";
  if (nodeName === "planner") return "📅 Building study plan…";
  if (nodeName === "quiz") return "🧩 Generating quiz…";
  if (nodeName === "diagram") return "📊 Building diagram…";
  if (nodeName === "code_fix") return "🔧 Analyzing code…";
  if (nodeName === "general") return "💬 Generating answer…";

  const clean = stripDecorativePrefix(fallbackLabel || "");
  return clean ? `⚙️ ${clean}…` : "⚙️ Processing step…";
}

function normalizeToolStepLabel(mode: string | null, toolName?: string, fallbackLabel?: string): string {
  const tool = (toolName || "").trim();

  if (mode === "thinking" && tool === "calculator") return "🔢 Running calculator…";
  if (mode === "thinking" && tool.includes("search")) return "🌐 Searching the web…";
  if (mode === "quiz" && tool === "corpus_search") return "📚 Retrieving supporting material…";

  if (tool === "calculator") return "🔢 Running calculator…";
  if (tool === "search_web") return "🌐 Searching the web…";
  if (tool === "search_arxiv") return "📄 Searching arXiv…";
  if (tool === "search_wikipedia") return "📖 Searching Wikipedia…";
  if (tool === "execute_python") return "⚙️ Executing code…";
  if (tool === "corpus_search") return "📚 Searching course materials…";

  const clean = stripDecorativePrefix(fallbackLabel || "");
  return clean ? `🔧 ${clean}…` : `🔧 Using ${tool || "tool"}…`;
}

function asNonEmptyString(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

function finalAnswerStepLabel(mode: string | null): string {
  if (mode === "explain") return "✍️ Generating final answer…";
  if (mode === "quiz") return "✅ Preparing quiz output…";
  if (mode === "roadmap") return "🗺️ Drafting study plan…";
  if (mode === "research") return "✅ Finalizing research report…";
  return "✍️ Generating response…";
}

function hasStepLabel(steps: StreamStep[], label: string): boolean {
  return steps.some((step) => step.label === label);
}

function shouldDedupeToolStep(mode: string | null, toolName: string): boolean {
  if (mode !== "research") return false;
  return RESEARCH_SEARCH_TOOLS.has(toolName);
}

export type StreamStep = {
  id: string;
  label: string;
  status: "active" | "done";
};

export type ArtifactInfo = {
  kind: string;
  filename: string;
  path: string;
  url?: string;
};

export interface StreamState {
  isStreaming: boolean;
  content: string;
  thinking: string;
  activeTool: string | null;
  error: string | null;
  steps: StreamStep[];
  artifact: ArtifactInfo | null;
  activeMode: string | null;
}

export interface StreamCompletePayload {
  finalContent: string;
  artifact: ArtifactInfo | null;
}

export function useChatStream() {
  const [streamState, setStreamState] = useState<StreamState>({
    isStreaming: false,
    content: "",
    thinking: "",
    activeTool: null,
    error: null,
    steps: [],
    artifact: null,
    activeMode: null,
  });

  const eventSourceRef = useRef<EventSource | null>(null);
  const cancelMockRef = useRef<(() => void) | null>(null);
  const streamedContentRef = useRef("");
  const latestArtifactRef = useRef<ArtifactInfo | null>(null);
  const queryClient = useQueryClient();

  const startStream = useCallback((
    threadId: string,
    activeMode?: string,
    onComplete?: (payload: StreamCompletePayload) => void,
  ) => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    if (cancelMockRef.current) {
      cancelMockRef.current();
      cancelMockRef.current = null;
    }

    streamedContentRef.current = "";
    latestArtifactRef.current = null;
    setStreamState({
      isStreaming: true,
      content: "",
      thinking: "",
      activeTool: null,
      error: null,
      steps: [],
      artifact: null,
      activeMode: activeMode || null,
    });

    if (IS_MOCK_ENABLED) {
      cancelMockRef.current = mockStream((chunk) => {
        streamedContentRef.current += chunk;
        setStreamState(prev => {
          const nextState: StreamState = {
            ...prev,
            content: prev.content + chunk,
          };

          if (chunk && !prev.content && TIMELINE_MODES.has(prev.activeMode || "")) {
            const newSteps: StreamStep[] = prev.steps.map(s => ({ ...s, status: "done" }));
            newSteps.push({
              id: `response-${Date.now()}`,
              label: finalAnswerStepLabel(prev.activeMode),
              status: "active",
            });
            nextState.steps = newSteps;
          }
          return nextState;
        });
      }, () => {
        const finalContent = streamedContentRef.current;
        cancelMockRef.current = null;
        setStreamState(prev => ({
          ...prev,
          isStreaming: false,
          content: "",
          thinking: prev.thinking,
          activeTool: null,
          steps: prev.steps.map(s => ({ ...s, status: "done" }))
        }));
        queryClient.invalidateQueries({ queryKey: getListSessionsQueryKey() });
        onComplete?.({ finalContent, artifact: latestArtifactRef.current });
      }, (data) => {
        const eventType = String((data as { type?: unknown })?.type ?? "");
        if (eventType === "heartbeat") return;
        if (eventType === 'artifact') {
          // Type-narrow for artifact event
          const artifactData = data as { kind?: string; filename?: string; path?: string; url?: string };
          const artifact: ArtifactInfo = {
            kind: asNonEmptyString(artifactData.kind) || "file",
            filename: asNonEmptyString(artifactData.filename) || "artifact",
            path: asNonEmptyString(artifactData.path) || "",
            url: asNonEmptyString(artifactData.url),
          };
          latestArtifactRef.current = artifact;
          setStreamState(prev => ({ ...prev, artifact }));
          return;
        }
        setStreamState(prev => {
          if (data.type === 'chunk') return prev;
          if (data.type === 'thinking') return prev;
          if (data.type === 'node_start') {
            const nodeName = asNonEmptyString((data as { node?: string }).node) || "unknown";
            const label = normalizeNodeStepLabel(prev.activeMode, nodeName, asNonEmptyString((data as { label?: string }).label));

            if (!label) return prev; // Skip hidden steps

            if (hasStepLabel(prev.steps, label)) {
              return { ...prev, activeTool: null };
            }
            const newSteps: StreamStep[] = prev.steps.map(s => ({ ...s, status: "done" }));
            newSteps.push({
              id: `${nodeName}-${Date.now()}`,
              label,
              status: "active",
            });
            return { ...prev, steps: newSteps, activeTool: null };
          }
          if (data.type === 'tool_call') {
            const toolName = asNonEmptyString((data as { name?: string }).name) || "tool";
            const label = normalizeToolStepLabel(prev.activeMode, toolName, asNonEmptyString((data as { label?: string }).label));
            if (shouldDedupeToolStep(prev.activeMode, toolName) && hasStepLabel(prev.steps, label)) {
              return { ...prev, activeTool: toolName };
            }
            const newSteps: StreamStep[] = prev.steps.map(s => ({ ...s, status: "done" }));
            newSteps.push({
              id: `tool-${toolName}-${Date.now()}`,
              label,
              status: "active",
            });
            return { ...prev, steps: newSteps, activeTool: toolName };
          }
          if (data.type === 'error') {
            return {
              ...prev,
              error: (data as { message?: string }).message ?? null,
              isStreaming: false,
              activeTool: null,
              steps: prev.steps.map(s => ({ ...s, status: "done" }))
            };
          }
          return prev;
        });
      });
      return;
    }

    const es = new EventSource(`/api/stream/${threadId}`);
    eventSourceRef.current = es;

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === "heartbeat") {
          return;
        }
        if (data.type === 'done') {
          es.close();
          eventSourceRef.current = null;
          const finalContent = streamedContentRef.current;
          setStreamState(prev => ({
            ...prev,
            isStreaming: false,
            content: "",
            thinking: prev.thinking,
            activeTool: null,
            steps: prev.steps.map(s => ({ ...s, status: "done" }))
          }));
          queryClient.invalidateQueries({ queryKey: getListSessionsQueryKey() });
          onComplete?.({ finalContent, artifact: latestArtifactRef.current });
          return;
        }

        if (data.type === 'artifact') {
          const artifact: ArtifactInfo = {
            kind: asNonEmptyString(data.kind) || "file",
            filename: asNonEmptyString(data.filename) || "artifact",
            path: asNonEmptyString(data.path) || "",
            url: asNonEmptyString(data.url),
          };
          latestArtifactRef.current = artifact;
          setStreamState(prev => ({ ...prev, artifact }));
          return;
        }

        setStreamState(prev => {
          if (data.type === 'chunk') {
            const chunkText = typeof data.text === 'string' ? data.text : '';
            streamedContentRef.current += chunkText;
            const nextState: StreamState = {
              ...prev,
              content: prev.content + chunkText,
            };

            if (chunkText && !prev.content && TIMELINE_MODES.has(prev.activeMode || "")) {
              const newSteps: StreamStep[] = prev.steps.map(s => ({ ...s, status: "done" }));
              newSteps.push({
                id: `response-${Date.now()}`,
                label: finalAnswerStepLabel(prev.activeMode),
                status: "active",
              });
              nextState.steps = newSteps;
            }
            return nextState;
          }
          if (data.type === 'thinking') return prev;
          if (data.type === 'node_start') {
            const nodeName = asNonEmptyString(data.node) || "unknown";
            const label = normalizeNodeStepLabel(prev.activeMode, nodeName, asNonEmptyString(data.label));

            if (!label) return prev; // Skip hidden steps

            if (hasStepLabel(prev.steps, label)) {
              return { ...prev, activeTool: null };
            }
            const newSteps: StreamStep[] = prev.steps.map(s => ({ ...s, status: "done" }));
            newSteps.push({
              id: `${nodeName}-${Date.now()}`,
              label,
              status: "active",
            });
            return { ...prev, steps: newSteps, activeTool: null };
          }
          if (data.type === 'tool_call') {
            const toolName = asNonEmptyString(data.name) || asNonEmptyString(data.tool_name) || "tool";
            const label = normalizeToolStepLabel(prev.activeMode, toolName, asNonEmptyString(data.label));
            if (shouldDedupeToolStep(prev.activeMode, toolName) && hasStepLabel(prev.steps, label)) {
              return { ...prev, activeTool: toolName };
            }
            const newSteps: StreamStep[] = prev.steps.map(s => ({ ...s, status: "done" }));
            newSteps.push({
              id: `tool-${toolName}-${Date.now()}`,
              label,
              status: "active",
            });
            return { ...prev, steps: newSteps, activeTool: toolName };
          }
          if (data.type === 'error') {
            es.close();
            eventSourceRef.current = null;
            return {
              ...prev,
              error: data.message ?? data.text ?? "An error occurred.",
              isStreaming: false,
              activeTool: null,
              steps: prev.steps.map(s => ({ ...s, status: "done" }))
            };
          }
          return prev;
        });
      } catch (err) {
        console.error("Failed to parse SSE message", err);
        es.close();
        eventSourceRef.current = null;
        setStreamState(prev => ({
          ...prev,
          error: "Received malformed stream data from server.",
          isStreaming: false,
          activeTool: null,
          steps: prev.steps.map(s => ({ ...s, status: "done" }))
        }));
      }
    };

    es.onerror = () => {
      es.close();
      eventSourceRef.current = null;
      setStreamState(prev => ({
        ...prev,
        error: "Connection lost.",
        isStreaming: false,
        activeTool: null,
        steps: prev.steps.map(s => ({ ...s, status: "done" }))
      }));
    };
  }, [queryClient]);

  const stopStream = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    if (cancelMockRef.current) {
      cancelMockRef.current();
      cancelMockRef.current = null;
    }
    setStreamState(prev => ({
      ...prev,
      isStreaming: false,
      content: "",
      thinking: "",
      activeTool: null,
      steps: prev.steps.map(s => ({ ...s, status: "done" }))
    }));
    latestArtifactRef.current = null;
  }, []);

  return { streamState, startStream, stopStream };
}