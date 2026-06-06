import React, { useState, useRef, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { SendHorizontal, Plus, ChevronRight, Check, X, Square, Timer } from "lucide-react";
import { SAGE_MODES, type SageMode, cn } from "@/lib/utils";
import { useGetCourses } from "@workspace/api-client-react";

// --- Diagram cooldown (3 minutes) ---
const DIAGRAM_COOLDOWN_MS = 3 * 60 * 1000;
const DIAGRAM_COOLDOWN_KEY = "sage_diagram_last_used";

function getDiagramCooldownRemaining(): number {
  const raw = localStorage.getItem(DIAGRAM_COOLDOWN_KEY);
  if (!raw) return 0;
  const elapsed = Date.now() - Number(raw);
  return Math.max(DIAGRAM_COOLDOWN_MS - elapsed, 0);
}

function markDiagramUsed(): void {
  localStorage.setItem(DIAGRAM_COOLDOWN_KEY, String(Date.now()));
}

function formatCooldown(ms: number): string {
  const s = Math.ceil(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

interface ComposerProps {
  onSend: (message: string, mode: SageMode, course: string) => void;
  onStopStreaming?: () => void;
  disabled: boolean;
  isStreaming?: boolean;
  selectedMode?: SageMode;
  onModeChange?: (mode: SageMode) => void;
  resetSelectionKey?: number;
  triggerDiagramCooldown?: number;
}

export function Composer({ onSend, onStopStreaming, disabled, isStreaming = false, selectedMode, onModeChange, resetSelectionKey, triggerDiagramCooldown }: ComposerProps) {
  const [message, setMessage] = useState("");
  const [mode, setMode] = useState<SageMode>("general");
  const [course, setCourse] = useState("all");
  const [modeSelectorOpen, setModeSelectorOpen] = useState(false);
  const [coursesOpen, setCoursesOpen] = useState(false);
  const [coursesOpenUpward, setCoursesOpenUpward] = useState(false);
  const [diagramCooldown, setDiagramCooldown] = useState(getDiagramCooldownRemaining);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const selectorRef = useRef<HTMLDivElement>(null);

  const { data: coursesData } = useGetCourses();
  const courses = [{ value: "all", label: "All" }, ...(coursesData?.courses || []).map((courseCode) => ({ value: courseCode, label: courseCode }))];

  const currentModeObj = SAGE_MODES.find(m => m.id === mode)!;
  const isComposerDisabled = disabled || isStreaming;
  const isDiagramOnCooldown = diagramCooldown > 0;

  // Tick the cooldown timer every second while active
  useEffect(() => {
    if (diagramCooldown <= 0) return;
    const id = window.setInterval(() => {
      const remaining = getDiagramCooldownRemaining();
      setDiagramCooldown(remaining);
    }, 1000);
    return () => window.clearInterval(id);
  }, [diagramCooldown > 0]);

  // Start cooldown when triggered by parent (gen finished)
  useEffect(() => {
    if (triggerDiagramCooldown && triggerDiagramCooldown > 0) {
      markDiagramUsed();
      setDiagramCooldown(DIAGRAM_COOLDOWN_MS);
    }
  }, [triggerDiagramCooldown]);

  useEffect(() => {
    if (selectedMode && selectedMode !== mode) {
      setMode(selectedMode);
    }
  }, [selectedMode, mode]);

  useEffect(() => {
    setCourse("all");
  }, [resetSelectionKey]);

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "24px"; // reset
      const scrollHeight = textareaRef.current.scrollHeight;
      textareaRef.current.style.height = Math.min(scrollHeight, 200) + "px";
    }
  }, [message]);

  // Click outside mode selector
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (selectorRef.current && !selectorRef.current.contains(e.target as Node)) {
        setModeSelectorOpen(false);
        setCoursesOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (message.trim() && !isComposerDisabled) {
        handleSendWithCooldown(message.trim(), mode, course);
      }
    }
  };

  const handleSendWithCooldown = (msg: string, sendMode: SageMode, sendCourse: string) => {
    onSend(msg, sendMode, sendCourse);
    setMessage("");
    if (textareaRef.current) textareaRef.current.style.height = "24px";
  };

  const setModeAndNotify = (nextMode: SageMode) => {
    setMode(nextMode);
    onModeChange?.(nextMode);
    
    const supportsCourses = ["explain", "quiz", "roadmap"].includes(nextMode);
    if (supportsCourses) {
      setCoursesOpen(true);
    } else {
      setModeSelectorOpen(false);
      setCoursesOpen(false);
    }
  };

  const clearModeSelection = () => {
    setModeAndNotify("general");
  };

  const selectedCourseLabel = courses.find((courseOption) => courseOption.value === course)?.label ?? "All";

  const setCourseAndClose = (nextCourse: string) => {
    setCourse(nextCourse);
    setModeSelectorOpen(false);
    setCoursesOpen(false);
  };

  return (
    <div className="relative w-full max-w-[750px] mx-auto px-4 md:px-8">
      <div className="flex items-center justify-between mb-2 px-1">
        <div className="flex items-center gap-2">
          <div className="inline-flex items-center gap-1.5 bg-sidebar border border-sidebar-border rounded-full px-2.5 py-1 text-xs text-foreground/90">
            <span className="text-muted-foreground">Mode:</span>
            <span className="font-medium">{currentModeObj.icon} {currentModeObj.name}</span>
            {mode === "diagram" && isDiagramOnCooldown && (
              <span className="ml-1 text-[10px] text-warning font-mono tabular-nums" title="Diagram cooldown active">
                <Timer className="inline w-3 h-3 mr-0.5 -mt-px" />
                {formatCooldown(diagramCooldown)}
              </span>
            )}
            {mode !== "general" && (
              <button
                type="button"
                onClick={clearModeSelection}
                disabled={isComposerDisabled}
                className="ml-0.5 rounded-full p-0.5 text-muted-foreground hover:text-foreground hover:bg-white/10 disabled:opacity-50 disabled:cursor-not-allowed"
                title="Clear mode"
                aria-label="Clear mode"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>

          {["explain", "quiz", "roadmap"].includes(mode) && (
            <div className="inline-flex items-center gap-1.5 bg-sidebar border border-sidebar-border rounded-full px-2.5 py-1 text-xs text-foreground/90">
              <span className="text-muted-foreground">Course:</span>
              <span className="font-medium">{selectedCourseLabel}</span>
            </div>
          )}
        </div>

        <AnimatePresence>
          {message.length > 0 && (
            <motion.div
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.9 }}
              transition={{ duration: 0.1 }}
              className={cn(
                "inline-flex items-center gap-1.5 bg-sidebar border rounded-full px-2.5 py-1 text-xs",
                message.length > 2200 ? "border-error/50 text-error" : "border-sidebar-border text-foreground/90"
              )}
            >
              <span className="text-muted-foreground">Length:</span>
              <span className="font-semibold">{message.length}/2200</span>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <div className="relative">
        <div className={cn(
          "relative flex items-end w-full rounded-[24px] bg-input border-2 transition-colors duration-200 shadow-lg shadow-black/20",
          isComposerDisabled ? "border-border/50 opacity-60" : "border-border focus-within:border-primary/50 focus-within:ring-4 focus-within:ring-primary/10"
        )}>

          <textarea
            ref={textareaRef}
            value={message}
            onChange={(e) => setMessage(e.target.value.slice(0, 2200))}
            onKeyDown={handleKeyDown}
            disabled={isComposerDisabled}
            placeholder={currentModeObj.placeholder}
            maxLength={2200}
            className="flex-1 max-h-[200px] min-h-[24px] py-3 pl-4 pr-2 bg-transparent text-foreground placeholder:text-muted-foreground outline-none resize-none overflow-y-auto"
            rows={1}
          />

          <div className="flex items-center justify-end p-1.5 pr-2.5 pb-1.5">
            <button
              onClick={() => {
                if (isStreaming) {
                  onStopStreaming?.();
                  return;
                }

                if (message.trim() && !isComposerDisabled) {
                  handleSendWithCooldown(message.trim(), mode, course);
                }
              }}
              disabled={isStreaming ? false : (isComposerDisabled || !message.trim() || (mode === "diagram" && isDiagramOnCooldown))}
              className={cn(
                "flex items-center justify-center w-10 h-10 rounded-full transition-all shadow-md",
                isStreaming
                  ? "bg-destructive text-destructive-foreground hover:opacity-90"
                  : "bg-primary text-primary-foreground hover:bg-primary/90 hover:scale-105 active:scale-95 disabled:opacity-50 disabled:hover:scale-100 disabled:cursor-not-allowed"
              )}
            >
              {isStreaming ? <Square className="w-4 h-4 fill-current" /> : <SendHorizontal className="w-5 h-5 ml-0.5" />}
            </button>
          </div>
        </div>

        <div className="absolute left-0 top-1/2 -translate-x-[calc(100%+8px)] -translate-y-1/2" ref={selectorRef}>
          <div className={cn(
            "relative w-10 h-10 flex items-center justify-center transition-all duration-200",
            isComposerDisabled ? "opacity-50" : "group hover:scale-110 active:scale-95 cursor-pointer"
          )}>
            {!isComposerDisabled && !modeSelectorOpen && (
              <div className="absolute inset-0 rounded-full bg-[conic-gradient(from_0deg,transparent_70%,hsl(var(--primary))_100%)] animate-spin" style={{ animationDuration: "3s" }} />
            )}

            <button
              onClick={() => {
                if (isComposerDisabled) return;
                setModeSelectorOpen((prev) => !prev);
                if (modeSelectorOpen) setCoursesOpen(false);
              }}
              disabled={isComposerDisabled}
              className={cn(
                "absolute inset-[1.5px] rounded-full flex items-center justify-center transition-all duration-200",
                isComposerDisabled
                  ? "bg-input text-muted-foreground border border-border"
                  : modeSelectorOpen
                    ? "bg-primary text-primary-foreground"
                    : "bg-input text-muted-foreground group-hover:text-foreground"
              )}
            >
              <Plus className="w-4 h-4" />
            </button>
          </div>

          <AnimatePresence>
            {modeSelectorOpen && (
              <motion.div
                initial={{ opacity: 0, y: 10, scale: 0.95 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: 10, scale: 0.95 }}
                transition={{ duration: 0.15 }}
                className="absolute bottom-14 left-0 w-52 bg-sidebar/85 backdrop-blur-md border border-white/10 rounded-2xl shadow-2xl overflow-visible z-50 py-1.5"
              >
                <div className="max-h-[136px] overflow-y-auto [&::-webkit-scrollbar]:hidden [-ms-overflow-style:none] [scrollbar-width:none] flex flex-col gap-0.5 py-0.5">
                  {SAGE_MODES.map((m) => {
                    const isCoolingDiagram = m.id === "diagram" && isDiagramOnCooldown;
                    const isSelected = mode === m.id;
                    return (
                      <button
                        key={m.id}
                        onClick={() => { if (!isCoolingDiagram) setModeAndNotify(m.id as SageMode); }}
                        disabled={isCoolingDiagram}
                        className={cn(
                          "mx-1.5 w-[calc(100%-12px)] rounded-xl px-2.5 py-1.5 flex items-center gap-3 text-left transition-all duration-200 shrink-0",
                          isCoolingDiagram
                            ? "opacity-40 cursor-not-allowed text-muted-foreground"
                            : isSelected
                              ? "bg-primary/10 text-primary border border-primary/20 font-semibold"
                              : "text-foreground hover:bg-white/[0.06] hover:translate-x-0.5"
                        )}
                      >
                        <span className="text-base bg-white/[0.04] p-1.5 rounded-lg border border-white/5 shrink-0 flex items-center justify-center w-8 h-8">
                          {m.icon}
                        </span>
                        <span className="font-semibold text-xs text-foreground/90 flex-1">{m.name}</span>
                        {isCoolingDiagram && (
                          <span className="ml-auto text-[10px] font-mono tabular-nums text-warning flex items-center gap-1 shrink-0">
                            <Timer className="w-3 h-3" />
                            {formatCooldown(diagramCooldown)}
                          </span>
                        )}
                        {!isCoolingDiagram && isSelected && (
                          <Check className="w-3.5 h-3.5 ml-auto text-primary shrink-0" />
                        )}
                      </button>
                    );
                  })}
                </div>

                <div className="h-px bg-white/5 my-1" />

                {["explain", "quiz", "roadmap"].includes(mode) && (
                  <div
                    className="relative"
                    onMouseEnter={(e) => {
                      const rect = (e.currentTarget as HTMLDivElement).getBoundingClientRect();
                      const estimatedSubmenuHeight = 220;
                      setCoursesOpenUpward(window.innerHeight - rect.top < estimatedSubmenuHeight);
                      setCoursesOpen(true);
                    }}
                    onMouseLeave={() => setCoursesOpen(false)}
                  >
                    <button
                      type="button"
                      className="mx-1.5 w-[calc(100%-12px)] rounded-xl px-2.5 py-1.5 flex items-center gap-3 text-left transition-all duration-200 hover:bg-white/[0.06] hover:translate-x-0.5 text-foreground"
                    >
                      <span className="text-base bg-white/[0.04] p-1.5 rounded-lg border border-white/5 shrink-0 flex items-center justify-center w-8 h-8">
                        📚
                      </span>
                      <span className="font-semibold text-xs text-foreground/90 flex-1">Courses</span>
                      <ChevronRight className="w-3.5 h-3.5 ml-auto text-muted-foreground/50 shrink-0" />
                    </button>

                    <AnimatePresence>
                      {coursesOpen && (
                        <motion.div
                          initial={{ opacity: 0, x: 8, scale: 0.98 }}
                          animate={{ opacity: 1, x: 0, scale: 1 }}
                          exit={{ opacity: 0, x: 8, scale: 0.98 }}
                          transition={{ duration: 0.12 }}
                          className={cn(
                            "absolute left-full ml-2 w-48 max-h-[11rem] overflow-y-auto custom-scrollbar pr-1 bg-sidebar/90 backdrop-blur-md border border-white/10 rounded-2xl shadow-2xl py-1.5 z-50",
                            coursesOpenUpward ? "bottom-0" : "top-0"
                          )}
                        >
                          {courses.map((courseOption) => (
                            <button
                              key={courseOption.value}
                              onClick={() => setCourseAndClose(courseOption.value)}
                              className={cn(
                                "mx-1.5 w-[calc(100%-12px)] rounded-lg px-2.5 py-1.5 flex items-center justify-between text-xs transition-all duration-200 hover:translate-x-0.5",
                                course === courseOption.value
                                  ? "bg-primary/10 text-primary border border-primary/20 font-semibold"
                                  : "text-foreground hover:bg-white/[0.06]"
                              )}
                            >
                              <span className="truncate">{courseOption.label}</span>
                              {course === courseOption.value && <Check className="w-3.5 h-3.5 text-primary shrink-0" />}
                            </button>
                          ))}
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </div>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
