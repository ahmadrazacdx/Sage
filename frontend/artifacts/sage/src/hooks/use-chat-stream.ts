import { useState, useCallback, useRef } from "react";
import { mockStream } from "@/api/mock";
import { useQueryClient } from "@tanstack/react-query";
import { getListSessionsQueryKey } from "@workspace/api-client-react";

export interface StreamState {
  isStreaming: boolean;
  content: string;
  thinking: string;
  activeTool: string | null;
  error: string | null;
}

export function useChatStream() {
  const [streamState, setStreamState] = useState<StreamState>({
    isStreaming: false,
    content: "",
    thinking: "",
    activeTool: null,
    error: null,
  });
  
  const eventSourceRef = useRef<EventSource | null>(null);
  const cancelMockRef = useRef<(() => void) | null>(null);
  const streamedContentRef = useRef("");
  const queryClient = useQueryClient();

  const startStream = useCallback((threadId: string, onComplete?: (finalContent: string) => void) => {
    streamedContentRef.current = "";
    setStreamState({
      isStreaming: true,
      content: "",
      thinking: "",
      activeTool: null,
      error: null,
    });

    if (import.meta.env.VITE_USE_MOCK !== 'false') {
      cancelMockRef.current = mockStream((chunk) => {
        streamedContentRef.current += chunk;
        setStreamState(prev => ({ ...prev, content: prev.content + chunk }));
      }, () => {
        const finalContent = streamedContentRef.current;
        cancelMockRef.current = null;
        setStreamState(prev => ({ ...prev, isStreaming: false, content: "", thinking: "", activeTool: null }));
        queryClient.invalidateQueries({ queryKey: getListSessionsQueryKey() });
        onComplete?.(finalContent);
      }, (data) => {
        setStreamState(prev => {
          if (data.type === 'chunk') return prev;
          if (data.type === 'thinking') return { ...prev, thinking: prev.thinking + data.text + "\n" };
          if (data.type === 'tool_call') return { ...prev, activeTool: data.name };
          if (data.type === 'error') return { ...prev, error: data.message, isStreaming: false };
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
          const finalContent = streamedContentRef.current;
          setStreamState(prev => ({ ...prev, isStreaming: false, content: "", thinking: "", activeTool: null }));
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
          if (data.type === 'tool_call') return { ...prev, activeTool: data.name ?? data.tool_name ?? null };
          if (data.type === 'error') {
            es.close();
            return { ...prev, error: data.message ?? data.text ?? "An error occurred.", isStreaming: false };
          }
          return prev;
        });
      } catch (err) {
        console.error("Failed to parse SSE message", err);
      }
    };

    es.onerror = () => {
      es.close();
      setStreamState(prev => ({ ...prev, error: "Connection lost.", isStreaming: false }));
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
    setStreamState(prev => ({ ...prev, isStreaming: false, content: "", thinking: "", activeTool: null }));
  }, []);

  return { streamState, startStream, stopStream };
}
