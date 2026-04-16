import React, { useState, useRef, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Send, Plus, ChevronRight, Check, X, Square } from "lucide-react";
import { SAGE_MODES, type SageMode, cn } from "@/lib/utils";
import { useGetCourses } from "@workspace/api-client-react";

interface ComposerProps {
  onSend: (message: string, mode: SageMode, course: string) => void;
  onStopStreaming?: () => void;
  disabled: boolean;
  isStreaming?: boolean;
  selectedMode?: SageMode;
  onModeChange?: (mode: SageMode) => void;
  resetSelectionKey?: number;
}

export function Composer({ onSend, onStopStreaming, disabled, isStreaming = false, selectedMode, onModeChange, resetSelectionKey }: ComposerProps) {
  const [message, setMessage] = useState("");
  const [mode, setMode] = useState<SageMode>("general");
  const [course, setCourse] = useState("all");
  const [modeSelectorOpen, setModeSelectorOpen] = useState(false);
  const [coursesOpen, setCoursesOpen] = useState(false);
  const [coursesOpenUpward, setCoursesOpenUpward] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const selectorRef = useRef<HTMLDivElement>(null);

  const { data: coursesData } = useGetCourses();
  const courses = [{ value: "all", label: "Default" }, ...(coursesData?.courses || []).map((courseCode) => ({ value: courseCode, label: courseCode }))];

  const currentModeObj = SAGE_MODES.find(m => m.id === mode)!;
  const isComposerDisabled = disabled || isStreaming;

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
        onSend(message.trim(), mode, course);
        setMessage("");
        if (textareaRef.current) textareaRef.current.style.height = "24px";
      }
    }
  };

  const setModeAndNotify = (nextMode: SageMode) => {
    setMode(nextMode);
    onModeChange?.(nextMode);
    setModeSelectorOpen(false);
    setCoursesOpen(false);
  };

  const clearModeSelection = () => {
    setModeAndNotify("general");
  };

  const selectedCourseLabel = courses.find((courseOption) => courseOption.value === course)?.label ?? "Default";

  const setCourseAndClose = (nextCourse: string) => {
    setCourse(nextCourse);
    setModeSelectorOpen(false);
    setCoursesOpen(false);
  };

  return (
    <div className="relative w-full max-w-[680px] mx-auto">
      <div className="flex items-center gap-2 mb-2 px-1">
        <div className="inline-flex items-center gap-1.5 bg-sidebar border border-sidebar-border rounded-full px-2.5 py-1 text-xs text-foreground/90">
          <span className="text-muted-foreground">Mode:</span>
          <span className="font-medium">{currentModeObj.icon} {currentModeObj.name}</span>
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

        <div className="inline-flex items-center gap-1.5 bg-sidebar border border-sidebar-border rounded-full px-2.5 py-1 text-xs text-foreground/90">
          <span className="text-muted-foreground">Course:</span>
          <span className="font-medium">{selectedCourseLabel}</span>
        </div>
      </div>

      <div className="relative">
        <div className={cn(
          "relative flex items-end w-full rounded-[24px] bg-input border-2 transition-colors duration-200 shadow-lg shadow-black/20",
          isComposerDisabled ? "border-border/50 opacity-60" : "border-border focus-within:border-primary/50 focus-within:ring-4 focus-within:ring-primary/10"
        )}>

          <textarea
            ref={textareaRef}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isComposerDisabled}
            placeholder={currentModeObj.placeholder}
            className="flex-1 max-h-[200px] min-h-[24px] py-4 pl-4 bg-transparent text-foreground placeholder:text-muted-foreground outline-none resize-none overflow-y-auto"
            rows={1}
          />

          <div className="p-2">
            <button
              onClick={() => {
                if (isStreaming) {
                  onStopStreaming?.();
                  return;
                }

                if (message.trim() && !isComposerDisabled) {
                  onSend(message.trim(), mode, course);
                  setMessage("");
                  if (textareaRef.current) textareaRef.current.style.height = "24px";
                }
              }}
              disabled={isStreaming ? false : (isComposerDisabled || !message.trim())}
              className={cn(
                "flex items-center justify-center w-10 h-10 rounded-full transition-all shadow-md",
                isStreaming
                  ? "bg-destructive text-destructive-foreground hover:opacity-90"
                  : "bg-accent text-accent-foreground hover:bg-white hover:scale-105 active:scale-95 disabled:opacity-50 disabled:hover:scale-100 disabled:cursor-not-allowed"
              )}
            >
              {isStreaming ? <Square className="w-4 h-4 fill-current" /> : <Send className="w-5 h-5 ml-0.5" />}
            </button>
          </div>
        </div>

        <div className="absolute left-0 top-1/2 -translate-x-[calc(100%+8px)] -translate-y-1/2" ref={selectorRef}>
          <button
            onClick={() => {
              if (isComposerDisabled) return;
              setModeSelectorOpen((prev) => !prev);
              if (modeSelectorOpen) setCoursesOpen(false);
            }}
            disabled={isComposerDisabled}
            className="flex items-center justify-center w-10 h-10 rounded-full bg-input border border-border hover:bg-white/5 text-muted-foreground hover:text-foreground transition-colors"
          >
            <Plus className="w-4 h-4" />
          </button>

          <AnimatePresence>
            {modeSelectorOpen && (
              <motion.div
                initial={{ opacity: 0, y: 10, scale: 0.95 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: 10, scale: 0.95 }}
                transition={{ duration: 0.15 }}
                className="absolute bottom-14 left-0 w-52 bg-sidebar border border-sidebar-border rounded-xl shadow-2xl overflow-visible z-50 py-1"
              >
                {SAGE_MODES.map((m) => (
                  <button
                    key={m.id}
                    onClick={() => { setModeAndNotify(m.id as SageMode); }}
                    className={cn(
                      "flex items-center gap-3 w-full px-3 py-2.5 text-sm text-left transition-colors",
                      mode === m.id ? "bg-primary/10 text-primary" : "text-foreground hover:bg-white/5"
                    )}
                  >
                    <span className="text-base">{m.icon}</span>
                    <span className="font-medium">{m.name}</span>
                    {mode === m.id && <Check className="w-3.5 h-3.5 ml-auto" />}
                  </button>
                ))}

                <div className="h-px bg-sidebar-border my-1" />

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
                    className="flex items-center gap-3 w-full px-3 py-2.5 text-sm text-left transition-colors text-foreground hover:bg-white/5"
                  >
                    <span className="text-base">📚</span>
                    <span className="font-medium">Courses</span>
                    <ChevronRight className="w-3.5 h-3.5 ml-auto" />
                  </button>

                  <AnimatePresence>
                    {coursesOpen && (
                      <motion.div
                        initial={{ opacity: 0, x: 8, scale: 0.98 }}
                        animate={{ opacity: 1, x: 0, scale: 1 }}
                        exit={{ opacity: 0, x: 8, scale: 0.98 }}
                        transition={{ duration: 0.12 }}
                        className={cn(
                          "absolute left-full ml-2 w-48 max-h-[11rem] overflow-y-auto custom-scrollbar pr-1 bg-sidebar border border-sidebar-border rounded-xl shadow-2xl py-1",
                          coursesOpenUpward ? "bottom-0" : "top-0"
                        )}
                      >
                        {courses.map((courseOption) => (
                          <button
                            key={courseOption.value}
                            onClick={() => setCourseAndClose(courseOption.value)}
                            className={cn(
                              "flex items-center w-full px-3 py-2 text-sm text-left transition-colors",
                              course === courseOption.value
                                ? "bg-primary/10 text-primary"
                                : "text-foreground hover:bg-white/5"
                            )}
                          >
                            <span>{courseOption.label}</span>
                            {course === courseOption.value && <Check className="w-3.5 h-3.5 ml-auto" />}
                          </button>
                        ))}
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
