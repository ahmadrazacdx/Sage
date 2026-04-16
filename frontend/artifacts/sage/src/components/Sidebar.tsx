import React, { useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { MessageSquare, Plus, File, Trash2, GraduationCap, ChevronLeft, ChevronDown, ChevronRight, Info, CheckCircle2, Loader2, Menu } from "lucide-react";
import { cn } from "@/lib/utils";
import { 
  useListSessions, 
  useDeleteSession, 
  useListDocuments, 
  useDeleteDocument, 
  useUploadDocuments,
  type SystemStatus
} from "@workspace/api-client-react";
import { differenceInCalendarDays } from "date-fns";
import { useQueryClient } from "@tanstack/react-query";
import { getListSessionsQueryKey, getListDocumentsQueryKey } from "@workspace/api-client-react";

interface SidebarProps {
  currentThreadId: string | null;
  onSelectThread: (id: string) => void;
  onNewChat: () => void;
  onOpenSettings: () => void;
  isCollapsed: boolean;
  setCollapsed: (v: boolean) => void;
  status?: SystemStatus;
}

export function Sidebar({ currentThreadId, onSelectThread, onNewChat, onOpenSettings, isCollapsed, setCollapsed, status }: SidebarProps) {
  const { data: sessions } = useListSessions();
  const { data: documents } = useListDocuments();
  type SessionItem = NonNullable<typeof sessions>[number];
  type SessionGroups = {
    today: SessionItem[];
    yesterday: SessionItem[];
    week: SessionItem[];
    lastMonth: SessionItem[];
  };
  
  const deleteSession = useDeleteSession();
  const deleteDocument = useDeleteDocument();
  const uploadDocuments = useUploadDocuments();
  const queryClient = useQueryClient();

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [docsExpanded, setDocsExpanded] = useState(false);

  // Group sessions by date
  const groupedSessions = (sessions || []).reduce((acc, session) => {
    const updatedAt = new Date(session.updated_at);
    const daysAgo = differenceInCalendarDays(new Date(), updatedAt);

    if (daysAgo === 0) acc.today.push(session);
    else if (daysAgo === 1) acc.yesterday.push(session);
    else if (daysAgo >= 2 && daysAgo <= 7) acc.week.push(session);
    else if (daysAgo >= 8 && daysAgo <= 30) acc.lastMonth.push(session);

    return acc;
  }, { today: [], yesterday: [], week: [], lastMonth: [] } as SessionGroups);

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files?.length) return;
    setUploading(true);
    try {
      const files = Array.from(e.target.files);
      await uploadDocuments.mutateAsync({ data: { files, course: "all" } });
      queryClient.invalidateQueries({ queryKey: getListDocumentsQueryKey() });
    } catch (err) {
      console.error("Upload failed", err);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const SessionList = ({ title, items }: { title: string, items: typeof sessions }) => {
    if (!items || items.length === 0) return null;
    return (
      <div className="mb-4">
        <h4 className="text-[10px] font-bold tracking-wider text-muted-foreground uppercase px-3 mb-2">{title}</h4>
        <div className="space-y-0.5">
          {items.map(s => (
            <div key={s.thread_id} className="group relative flex items-center">
              <button
                onClick={() => onSelectThread(s.thread_id)}
                className={cn(
                  "flex-1 flex items-center gap-2 px-3 py-2 text-sm rounded-lg transition-all text-left truncate",
                  currentThreadId === s.thread_id 
                    ? "bg-primary/15 text-primary font-medium" 
                    : "text-foreground/80 hover:bg-white/5 hover:text-foreground"
                )}
              >
                <MessageSquare className="w-4 h-4 shrink-0 opacity-70" />
                <span className="truncate">{s.title || "New Conversation"}</span>
              </button>
              <button 
                onClick={(e) => {
                  e.stopPropagation();
                  deleteSession.mutate({ threadId: s.thread_id }, {
                    onSuccess: () => {
                      queryClient.invalidateQueries({ queryKey: getListSessionsQueryKey() });
                      if (currentThreadId === s.thread_id) onNewChat();
                    }
                  });
                }}
                className="absolute right-2 p-1.5 rounded-md text-muted-foreground hover:text-error hover:bg-error/10 opacity-0 group-hover:opacity-100 transition-all"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      </div>
    );
  };

  if (isCollapsed) {
    return (
      <button 
        onClick={() => setCollapsed(false)}
        className="fixed top-4 left-4 z-50 p-2 rounded-xl bg-sidebar border border-sidebar-border shadow-lg text-foreground hover:bg-white/5 transition-colors"
      >
        <Menu className="w-5 h-5" />
      </button>
    );
  }

  return (
    <motion.div 
      initial={{ x: -260 }}
      animate={{ x: 0 }}
      exit={{ x: -260 }}
      transition={{ type: "spring", stiffness: 300, damping: 30 }}
      className="flex flex-col w-[260px] h-screen bg-sidebar border-r border-sidebar-border shrink-0 text-foreground overflow-hidden"
    >
      {/* Header */}
      <div className="flex items-start justify-between p-4 shrink-0">
        <div className="flex items-start gap-2">
          <GraduationCap className="w-6 h-6 text-primary mt-0.5" />
          <div className="leading-tight">
            <div className="font-bold text-lg tracking-tight bg-gradient-to-br from-white to-white/60 bg-clip-text text-transparent">Sage</div>
            <div className="text-[11px] text-muted-foreground">Thal University Bhakkar</div>
          </div>
        </div>
        <button 
          onClick={() => setCollapsed(true)}
          className="p-1.5 rounded-lg hover:bg-white/10 text-muted-foreground hover:text-foreground transition-colors"
        >
          <ChevronLeft className="w-5 h-5" />
        </button>
      </div>

      {/* New Chat Button */}
      <div className="px-3 pb-4 shrink-0">
        <button 
          onClick={onNewChat}
          className="flex items-center gap-2 w-full px-3 py-2.5 bg-primary/10 hover:bg-primary/20 text-primary border border-primary/20 rounded-xl font-medium transition-all hover:scale-[1.02] active:scale-[0.98]"
        >
          <Plus className="w-4 h-4" />
          New Chat
        </button>
      </div>

      {/* Scrollable Content */}
      <div className="flex-1 overflow-y-auto overflow-x-hidden custom-scrollbar pb-4">
        <div className="px-2">
          <SessionList title="Today" items={groupedSessions.today} />
          <SessionList title="Yesterday" items={groupedSessions.yesterday} />
          <SessionList title="This Week" items={groupedSessions.week} />
          <SessionList title="Last Month" items={groupedSessions.lastMonth} />
        </div>

        <div className="px-4 py-4 mt-2">
          <div className="h-px w-full bg-sidebar-border mb-4" />

          <button
            type="button"
            onClick={() => setDocsExpanded((prev) => !prev)}
            className={cn(
              "w-full flex items-center justify-between px-3 py-2.5 rounded-xl border transition-colors",
              docsExpanded
                ? "bg-primary/12 border-primary/35"
                : "bg-white/[0.03] border-sidebar-border hover:bg-white/[0.06]"
            )}
          >
            <div className="flex items-center gap-2.5">
              <span className="text-base leading-none" aria-hidden="true">📁</span>
              <span className="text-xs font-extrabold tracking-wider text-foreground uppercase">MY DOCS</span>
              <span className="inline-flex items-center justify-center min-w-5 h-5 px-1 rounded-full bg-primary/15 text-primary text-[10px] font-bold">
                {documents?.length ?? 0}
              </span>
            </div>
            {docsExpanded ? <ChevronDown className="w-4 h-4 text-muted-foreground" /> : <ChevronRight className="w-4 h-4 text-muted-foreground" />}
          </button>

          <input
            type="file"
            ref={fileInputRef}
            className="hidden"
            multiple
            accept=".pdf,.docx,.pptx,.md,.txt"
            onChange={handleFileUpload}
          />

          {docsExpanded && (
            <div className="mt-2">
              <div className="flex items-center justify-end mb-2">
                <button
                  onClick={() => fileInputRef.current?.click()}
                  className="p-1 rounded bg-white/5 hover:bg-white/10 text-muted-foreground hover:text-foreground transition-colors"
                  title="Upload Document"
                >
                  <Plus className="w-3.5 h-3.5" />
                </button>
              </div>

              {uploading && (
                <div className="flex items-center gap-2 text-xs text-primary bg-primary/10 p-2 rounded-lg mb-2">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  Uploading...
                </div>
              )}

              <div className="space-y-1">
                {documents?.length === 0 && !uploading && (
                  <div className="text-xs text-muted-foreground italic px-1 py-2 text-center">No documents uploaded</div>
                )}
                {documents?.map(doc => (
                  <div key={doc.file} className="group relative flex items-center gap-2 px-2 py-1.5 text-xs rounded-md hover:bg-white/5 transition-colors">
                    <File className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                    <span className="truncate flex-1" title={doc.file}>{doc.file}</span>
                    <button
                      onClick={() => {
                        deleteDocument.mutate({ filename: doc.file }, {
                          onSuccess: () => queryClient.invalidateQueries({ queryKey: getListDocumentsQueryKey() })
                        });
                      }}
                      className="p-1 rounded text-muted-foreground hover:text-error opacity-0 group-hover:opacity-100 transition-opacity bg-sidebar"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          <button
            type="button"
            onClick={onOpenSettings}
            className="w-full mt-3 flex items-center gap-2 px-3 py-2 rounded-lg bg-white/[0.03] border border-sidebar-border text-sm text-foreground/90 hover:bg-white/[0.08] transition-colors"
          >
            <Info className="w-4 h-4 text-primary" />
            <span className="font-semibold tracking-wide">About</span>
          </button>
        </div>
      </div>

      {/* Footer Status */}
      <div className="p-4 border-t border-sidebar-border shrink-0 bg-sidebar">
        <div className="flex items-center justify-center gap-2 text-xs font-medium text-center">
          {status?.model_ready ? (
            <>
              <CheckCircle2 className="w-4 h-4 text-success" />
              <span className="text-foreground/80">Model Connected</span>
            </>
          ) : (
            <>
              <Loader2 className="w-4 h-4 text-warning animate-spin" />
              <span className="text-warning">Getting Things Ready...</span>
            </>
          )}
        </div>
      </div>
    </motion.div>
  );
}
