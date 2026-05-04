import React, { useState, useEffect, useRef, useCallback, Suspense, lazy } from "react";
import { Sidebar } from "@/components/Sidebar";
import { Composer } from "@/components/Composer";
import { WelcomeScreen } from "@/components/chat/WelcomeScreen";
import { FirstRun } from "@/components/setup/FirstRun";
import { ModelLoading } from "@/components/setup/ModelLoading";
import { useChatStream, type ArtifactInfo } from "@/hooks/use-chat-stream";
import { useGetStatus, useGetSessionMessages, useSubmitChat, type SystemStatus } from "@workspace/api-client-react";
import { GraduationCap, Loader2, X, Info, Github, Building2, Cpu, Database, WifiOff, ShieldCheck, FileDown } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { type SageMode } from "@/lib/utils";
import { useQueryClient } from "@tanstack/react-query";
import { getListSessionsQueryKey, getGetStatusQueryKey, getGetSessionMessagesQueryKey } from "@workspace/api-client-react";

const Markdown = lazy(() => import("@/components/Markdown").then((m) => ({ default: m.Markdown })));
const ProgressTimeline = lazy(() =>
  import("../components/ProgressTimeline").then((m) => ({ default: m.ProgressTimeline })),
);

interface LocalMessage {
  role: "user" | "assistant";
  content: string;
  artifact?: ArtifactInfo | null;
}

const TIMELINE_MODES = new Set<SageMode>(["quiz", "roadmap", "explain", "research", "diagram", "fix"]);

const BACKEND_MODE_MAP: Record<SageMode, string> = {
  general: "general",
  explain: "explain",
  thinking: "thinking",
  quiz: "quiz",
  diagram: "diagram",
  roadmap: "roadmap",
  fix: "fix",
  research: "research",
};

export default function Home() {
  const [userName, setUserName] = useState<string | null>(null);
  const [isFirstRunCheckDone, setIsFirstRunCheckDone] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [currentThreadId, setCurrentThreadId] = useState<string | null>(null);
  const [optimisticMessages, setOptimisticMessages] = useState<LocalMessage[]>([]);
  const [isStreamDone, setIsStreamDone] = useState(false);
  const [composerMode, setComposerMode] = useState<SageMode>("general");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [chatResetKey, setChatResetKey] = useState(0);
  const [stoppedManually, setStoppedManually] = useState(false);
  const [lastCompletedArtifact, setLastCompletedArtifact] = useState<ArtifactInfo | null>(null);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [settingsName, setSettingsName] = useState("");
  const [modelReady, setModelReady] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);
  const queryClient = useQueryClient();

  const { streamState, startStream, stopStream } = useChatStream();

  const { data: status } = useGetStatus({
    query: {
      queryKey: getGetStatusQueryKey(),
      refetchInterval: (query) =>
        (query.state.data as SystemStatus | undefined)?.model_ready ? 15000 : 2000,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
    },
  });

  const { data: historyMessages } = useGetSessionMessages(currentThreadId || "", {
    query: {
      queryKey: getGetSessionMessagesQueryKey(currentThreadId || ""),
      enabled: !!currentThreadId && isStreamDone,
    }
  });

  const submitChat = useSubmitChat();

  useEffect(() => {
    const savedName = localStorage.getItem("sage_user_name");
    const savedSidebar = localStorage.getItem("sage_sidebar_collapsed");
    if (savedName) setUserName(savedName);
    if (savedSidebar === "true") setSidebarCollapsed(true);
    setIsFirstRunCheckDone(true);
  }, []);

  useEffect(() => {
    if (status?.model_ready) {
      setModelReady(true);
    }
  }, [status?.model_ready]);

  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;

    const handleScroll = () => {
      const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 120;
      autoScrollRef.current = nearBottom;
    };

    handleScroll();
    container.addEventListener("scroll", handleScroll, { passive: true });
    return () => container.removeEventListener("scroll", handleScroll);
  }, []);

  const handleSidebarToggle = (collapsed: boolean) => {
    setSidebarCollapsed(collapsed);
    localStorage.setItem("sage_sidebar_collapsed", String(collapsed));
  };

  const handleOpenSettings = useCallback(() => {
    setSettingsName(userName || "");
    setIsSettingsOpen(true);
  }, [userName]);

  const handleSaveSettings = useCallback(() => {
    const trimmed = settingsName.trim();
    if (!trimmed) return;
    localStorage.setItem("sage_user_name", trimmed);
    setUserName(trimmed);
    setIsSettingsOpen(false);
  }, [settingsName]);

  useEffect(() => {
    if (!autoScrollRef.current) return;
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: streamState.isStreaming ? "auto" : "smooth" });
    }
  }, [historyMessages, optimisticMessages, streamState.content, streamState.isStreaming]);

  const handleSelectThread = useCallback((threadId: string) => {
    setCurrentThreadId(threadId);
    setOptimisticMessages([]);
    setLastCompletedArtifact(null);
    setIsStreamDone(true);
    setSubmitError(null);
    setStoppedManually(false);
  }, []);

  const handleNewChat = useCallback(() => {
    setCurrentThreadId(null);
    setOptimisticMessages([]);
    setLastCompletedArtifact(null);
    setIsStreamDone(false);
    setSubmitError(null);
    setStoppedManually(false);
    setChatResetKey((prev) => prev + 1);
  }, []);

  const handleSend = async (message: string, mode: SageMode, course: string) => {
    setSubmitError(null);
    setStoppedManually(false);
    setLastCompletedArtifact(null);
    setIsStreamDone(false);
    const backendMode = BACKEND_MODE_MAP[mode] ?? mode;

    const baseMessages: LocalMessage[] = historyList && canUseHistory
      ? historyList
      : optimisticMessages;
    setOptimisticMessages([...baseMessages, { role: "user", content: message }]);

    try {
      const response = await submitChat.mutateAsync({
        data: { thread_id: currentThreadId, message, mode: backendMode, course }
      });

      setCurrentThreadId(response.thread_id);

      startStream(response.thread_id, mode, ({ finalContent, artifact }) => {
        const messageText = finalContent.trim();
        setLastCompletedArtifact(artifact ?? null);
        if (messageText || artifact) {
          setOptimisticMessages(prev => [
            ...prev,
            {
              role: "assistant",
              content: messageText || "I have completed the report. You can download the PDF below.",
              artifact,
            },
          ]);
        }
        setIsStreamDone(true);
        queryClient.invalidateQueries({ queryKey: getListSessionsQueryKey() });
      });
    } catch (err) {
      const error = err as {
        status?: number;
        data?: { detail?: string };
        message?: string;
      };
      if (error?.status === 503) {
        setSubmitError(error.data?.detail ?? "Model is loading, please wait.");
      } else {
        setSubmitError(error?.data?.detail ?? error?.message ?? "Failed to send message.");
      }
      setIsStreamDone(true);
    }
  };

  const handleStopGeneration = useCallback(() => {
    if (!streamState.isStreaming) return;

    const partialContent = streamState.content;
    stopStream();
    setStoppedManually(true);

    if (partialContent.trim()) {
      setOptimisticMessages((prev) => [...prev, { role: "assistant", content: partialContent }]);
    }

    setIsStreamDone(true);
    queryClient.invalidateQueries({ queryKey: getListSessionsQueryKey() });
  }, [streamState.isStreaming, streamState.content, stopStream, queryClient]);

  const historyList = (historyMessages as LocalMessage[] | undefined) ?? undefined;
  const canUseHistory =
    isStreamDone &&
    !stoppedManually &&
    !!historyList &&
    (optimisticMessages.length === 0 || historyList.length >= optimisticMessages.length);

  useEffect(() => {
    if (canUseHistory) {
      setOptimisticMessages([]);
    }
  }, [canUseHistory]);

  if (!isFirstRunCheckDone) return null;
  if (!userName) return <FirstRun onComplete={setUserName} />;

  const isModelLoading = !modelReady;

  const displayMessages: LocalMessage[] = canUseHistory && historyList
    ? (() => {
        if (!lastCompletedArtifact || historyList.length === 0) return historyList;
        const merged = [...historyList];
        const lastIdx = merged.length - 1;
        if (merged[lastIdx]?.role === "assistant") {
          merged[lastIdx] = { ...merged[lastIdx], artifact: lastCompletedArtifact };
        }
        return merged;
      })()
    : optimisticMessages;

  const timelineVisible =
    TIMELINE_MODES.has((streamState.activeMode as SageMode) || "general") &&
    (streamState.isStreaming || streamState.error) &&
    streamState.steps?.length > 0;
  const showThinkingSpinner = streamState.isStreaming && streamState.activeMode === "thinking";

  const hasMessages = displayMessages.length > 0 || streamState.isStreaming || !!streamState.content;

  return (
    <div className="flex h-screen w-full bg-background overflow-hidden selection:bg-primary/30">
      {isModelLoading && <ModelLoading />}

      <AnimatePresence initial={false}>
        {!sidebarCollapsed && (
          <Sidebar
            currentThreadId={currentThreadId}
            onSelectThread={handleSelectThread}
            onNewChat={handleNewChat}
            onOpenSettings={handleOpenSettings}
            isCollapsed={sidebarCollapsed}
            setCollapsed={handleSidebarToggle}
            status={status}
          />
        )}
      </AnimatePresence>

      {sidebarCollapsed && (
        <button
          onClick={() => handleSidebarToggle(false)}
          className="fixed top-4 left-4 z-50 p-2 rounded-xl bg-sidebar border border-sidebar-border shadow-lg text-foreground hover:bg-white/5 transition-colors"
          title="Open sidebar"
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
      )}

      <main className="flex-1 flex flex-col h-full relative min-w-0">
        <div ref={scrollContainerRef} className="flex-1 overflow-y-auto custom-scrollbar pb-36">
          {!hasMessages ? (
            <WelcomeScreen name={userName} />
          ) : (
            <div className="w-full max-w-[980px] mx-auto px-4 md:px-8 py-8 flex flex-col gap-6">
              {displayMessages.map((msg, idx) => (
                <motion.div
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.15, ease: "easeOut" }}
                  key={`${currentThreadId}-${idx}`}
                  className={`flex w-full ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
                >
                  {msg.role === 'user' ? (
                    <div className="user-bubble px-5 py-3.5 max-w-[80%] shadow-md">
                      <p className="whitespace-pre-wrap text-[#e0e0e0] text-[1.2rem] leading-[1.75]">{msg.content}</p>
                    </div>
                  ) : (
                    <div className="flex gap-4 max-w-full flex-1">
                      <div className="w-8 h-8 shrink-0 rounded-full bg-sidebar border border-sidebar-border flex items-center justify-center mt-1">
                        <GraduationCap className="w-4 h-4 text-primary" />
                      </div>
                      <div className="flex-1 min-w-0 pt-1">
                        {/* Error crash guard */}
                        {msg.content?.includes("💣") || msg.content?.startsWith("<!") ? (
                          <div className="p-4 bg-red-500/10 text-red-300 border border-red-500/20 rounded-md text-sm my-4 font-mono">
                            ⚠️ An error occurred processing this response.
                          </div>
                        ) : (
                          <Suspense fallback={<p className="whitespace-pre-wrap text-[#e0e0e0]">{msg.content}</p>}>
                            <Markdown content={msg.content} enableMermaid={false} />
                          </Suspense>
                        )}

                        {msg.artifact && (
                          <div className="mt-4 px-4 py-3 border border-border rounded-xl bg-sidebar/50 flex items-center gap-3">
                            <FileDown className="w-4 h-4 text-primary shrink-0" />
                            <span className="text-sm font-medium truncate flex-1">{msg.artifact.filename}</span>
                            <a
                              href={msg.artifact.url || `/api/artifacts/${msg.artifact.filename}`}
                              download={msg.artifact.filename}
                              className="bg-primary hover:bg-primary/90 text-primary-foreground px-3 py-1.5 rounded-lg text-xs font-medium transition-colors shrink-0"
                            >
                              Save {msg.artifact.kind.toUpperCase()}
                            </a>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </motion.div>
              ))}

              {(streamState.isStreaming || streamState.error) && (
                <motion.div
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.15, ease: "easeOut" }}
                  className="flex w-full justify-start gap-4 flex-1"
                >
                  <div className="relative w-8 h-8 shrink-0 rounded-full bg-primary/10 border border-primary/20 flex items-center justify-center mt-1">
                    {showThinkingSpinner && (
                      <Loader2 className="absolute -inset-1.5 w-11 h-11 text-primary/40 animate-spin" />
                    )}
                    <GraduationCap className="w-4 h-4 text-primary" />
                  </div>
                  <div className="flex-1 min-w-0 pt-1 flex flex-col gap-2">
                    {timelineVisible && (
                      <Suspense fallback={null}>
                        <ProgressTimeline steps={streamState.steps} isComplete={!streamState.isStreaming} />
                      </Suspense>
                    )}

                    {streamState.error ? (
                      <div className="text-error text-sm p-3 bg-error/10 border border-error/20 rounded-lg">
                        ⚠️ {streamState.error}
                      </div>
                    ) : (
                      <div
                        className={
                          streamState.isStreaming && !streamState.content
                            ? (streamState.activeMode === "thinking" ? "" : "typing-cursor-empty")
                            : (streamState.isStreaming ? "typing-cursor" : "")
                        }
                      >
                        {streamState.content ? (
                          <Suspense fallback={<p className="whitespace-pre-wrap text-[#e0e0e0]">{streamState.content}</p>}>
                            <Markdown content={streamState.content} enableMermaid={false} />
                          </Suspense>
                        ) : streamState.isStreaming && streamState.activeMode === "thinking" ? (
                          <span className="text-muted-foreground text-sm">Thinking...</span>
                        ) : null}
                      </div>
                    )}
                    
                  </div>
                </motion.div>
              )}

              {submitError && (
                <div className="text-error text-sm p-3 bg-error/10 border border-error/20 rounded-lg">
                  ⚠️ {submitError}
                </div>
              )}

              <div ref={messagesEndRef} className="h-2" />
            </div>
          )}
        </div>

        <div className="absolute bottom-0 left-0 right-0 p-4 bg-gradient-to-t from-background via-background/95 to-transparent">
          <Composer
            onSend={handleSend}
            onStopStreaming={handleStopGeneration}
            selectedMode={composerMode}
            onModeChange={setComposerMode}
            resetSelectionKey={chatResetKey}
            isStreaming={streamState.isStreaming}
            disabled={submitChat.isPending || isModelLoading}
          />
        </div>
      </main>

      <AnimatePresence>
        {isSettingsOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-[70] bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
            onClick={() => setIsSettingsOpen(false)}
          >
            <motion.div
              initial={{ opacity: 0, y: 12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, scale: 0.98 }}
              transition={{ duration: 0.18 }}
              className="w-full max-w-xl bg-sidebar border border-sidebar-border rounded-2xl shadow-2xl"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between px-6 py-4 border-b border-sidebar-border">
                <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
                  <Info className="w-5 h-5 text-primary" />
                  ABOUT
                </h2>
                <button
                  type="button"
                  onClick={() => setIsSettingsOpen(false)}
                  className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-white/10 transition-colors"
                  aria-label="Close settings"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>

              <div className="px-6 py-5 space-y-5">
                <div className="space-y-3 text-sm text-foreground/85 leading-relaxed">
                  <p>
                    <b>Sage</b> is an offline AI Academic assistant designed for higher-education assistance at Thal University Bhakkar.
                    It combines guided tutoring, interactive study modes, and course-aware context to support daily learning with low friction.
                  </p>
                  <p>
                    The platform prioritizes production-grade reliability through controlled mode/course selection,
                    local personalization, and privacy-first behavior suitable for campus labs and personal systems.
                  </p>
                  <p className="text-xs text-muted-foreground">
                    License: <span className="font-semibold text-foreground/90">Apache 2.0</span>
                  </p>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 text-xs">
                  <div className="flex items-center gap-2 rounded-lg border border-sidebar-border bg-white/[0.03] px-3 py-2">
                    <Cpu className="w-3.5 h-3.5 text-primary" />
                    <span className="text-muted-foreground">Model:</span>
                    <span className="font-medium text-foreground/90 ml-auto">{status?.model_name || "Unknown"}</span>
                  </div>
                  <div className="flex items-center gap-2 rounded-lg border border-sidebar-border bg-white/[0.03] px-3 py-2">
                    <WifiOff className="w-3.5 h-3.5 text-primary" />
                    <span className="text-muted-foreground">Network:</span>
                    <span className="font-medium text-foreground/90 ml-auto">{status?.network_online ? "Online" : "Offline"}</span>
                  </div>
                </div>

                <div className="space-y-2">
                  <label htmlFor="settings-name" className="text-sm font-medium text-foreground">
                    Display Name
                  </label>
                  <input
                    id="settings-name"
                    type="text"
                    value={settingsName}
                    onChange={(e) => setSettingsName(e.target.value)}
                    placeholder="Enter your name"
                    className="w-full rounded-xl bg-input border border-border px-3 py-2.5 text-sm text-foreground placeholder:text-muted-foreground outline-none focus:border-primary/60"
                  />
                </div>

                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <a href="https://github.com/ahmadrazacdx/Sage" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-white/[0.04] border border-sidebar-border hover:bg-white/[0.1] transition-colors">
                    <Github className="w-3.5 h-3.5" />
                    GitHub
                  </a>
                  <a href="https://tu.edu.pk" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-white/[0.04] border border-sidebar-border hover:bg-white/[0.1] transition-colors">
                    <Building2 className="w-3.5 h-3.5" />
                    University
                  </a>
                </div>
              </div>

              <div className="px-6 py-4 border-t border-sidebar-border flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setIsSettingsOpen(false)}
                  className="px-4 py-2 rounded-lg text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-white/10 transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleSaveSettings}
                  disabled={!settingsName.trim()}
                  className="px-4 py-2 rounded-lg text-sm font-semibold bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
                >
                  Save Changes
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}