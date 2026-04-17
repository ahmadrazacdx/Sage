import { useState, useCallback, useRef } from "react";
import { IS_MOCK_ENABLED, mockStream } from "@/api/mock";
import { useQueryClient } from "@tanstack/react-query";
import { getListSessionsQueryKey } from "@workspace/api-client-react";

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
  const queryClient = useQueryClient();

  const startStream = useCallback((threadId: string, activeMode?: string, onComplete?: (finalContent: string) => void) => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    if (cancelMockRef.current) {
      cancelMockRef.current();
      cancelMockRef.current = null;
    }

    streamedContentRef.current = "";
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
        setStreamState(prev => ({ ...prev, content: prev.content + chunk }));
      }, () => {
        const finalContent = streamedContentRef.current;
        cancelMockRef.current = null;
        setStreamState(prev => ({ 
          ...prev, 
          isStreaming: false, 
          content: "", 
          thinking: "", 
          activeTool: null,
          steps: prev.steps.map(s => ({ ...s, status: "done" }))
        }));
        queryClient.invalidateQueries({ queryKey: getListSessionsQueryKey() });
        onComplete?.(finalContent);
      }, (data) => {
        setStreamState(prev => {
          if (data.type === 'chunk') return prev;
          if (data.type === 'thinking') return { ...prev, thinking: prev.thinking + data.text + "\n" };
          if (data.type === 'node_start') {
             const newSteps: StreamStep[] = prev.steps.map(s => ({ ...s, status: "done" }));
             newSteps.push({ id: data.node, label: data.label, status: "active" });
             return { ...prev, steps: newSteps, activeTool: null };
          }
          if (data.type === 'tool_call') {
             const newSteps: StreamStep[] = prev.steps.map(s => ({ ...s, status: "done" }));
             newSteps.push({ id: `tool-${data.name}-${Date.now()}`, label: data.label || `🔧 ${data.name}...`, status: "active" });
             return { ...prev, steps: newSteps, activeTool: data.name };
          }
          if (data.type === 'artifact') return { ...prev, artifact: data };
          if (data.type === 'error') {
            return {
              ...prev,
              error: data.message,
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
        if (data.type === 'done') {
          es.close();
          eventSourceRef.current = null;
          const finalContent = streamedContentRef.current;
          setStreamState(prev => ({ 
            ...prev, 
            isStreaming: false, 
            content: "", 
            thinking: "", 
            activeTool: null,
            steps: prev.steps.map(s => ({ ...s, status: "done" }))
          }));
          queryClient.invalidateQueries({ queryKey: getListSessionsQueryKey() });
          onComplete?.(finalContent);
          return;
        }

        setStreamState(prev => {
          if (data.type === 'chunk') {
            const chunkText = typeof data.text === 'string' ? data.text : '';
            streamedContentRef.current += chunkText;
            return { ...prev, content: prev.content + chunkText };
          }
          if (data.type === 'thinking') return { ...prev, thinking: prev.thinking + data.text + "\n" };
          if (data.type === 'node_start') {
             const newSteps: StreamStep[] = prev.steps.map(s => ({ ...s, status: "done" }));
             newSteps.push({ id: data.node, label: data.label, status: "active" });
             return { ...prev, steps: newSteps, activeTool: null };
          }
          if (data.type === 'tool_call') {
             const newSteps: StreamStep[] = prev.steps.map(s => ({ ...s, status: "done" }));
             newSteps.push({ id: `tool-${data.name}-${Date.now()}`, label: data.label || `🔧 ${data.name}...`, status: "active" });
             return { ...prev, steps: newSteps, activeTool: data.name ?? data.tool_name ?? null };
          }
          if (data.type === 'artifact') return { ...prev, artifact: data };
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
  }, []);

  return { streamState, startStream, stopStream };
}
