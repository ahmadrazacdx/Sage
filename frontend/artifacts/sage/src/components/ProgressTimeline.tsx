import React, { useState } from "react";
import { Check, Loader2, FileText, Globe, BookOpen } from "lucide-react";
import { StreamStep } from "@/hooks/use-chat-stream";

interface ProgressTimelineProps {
  steps: StreamStep[];
  isComplete: boolean;
}

function StepLabelWithLogo({ label }: { label: string }) {
  const isArxiv = label.toLowerCase().includes("arxiv");
  const isWeb = label.toLowerCase().includes("the web") || label.toLowerCase().includes("google");
  const isWiki = label.toLowerCase().includes("wikipedia");

  const [imgFailed, setImgFailed] = useState(false);
  const cleanLabel = label.replace(/^[^\p{L}\p{N}\s]+\s*/u, "");

  if (isArxiv) {
    return (
      <div className="flex items-center gap-1.5">
        {!imgFailed ? (
          <img
            src="/arxiv.png"
            alt=""
            className="w-4.5 h-4.5 object-contain shrink-0"
            onError={() => setImgFailed(true)}
          />
        ) : (
          <FileText className="w-4 h-4 text-muted-foreground/70 shrink-0" />
        )}
        <span>{cleanLabel}</span>
      </div>
    );
  }

  if (isWeb) {
    return (
      <div className="flex items-center gap-1.5">
        {!imgFailed ? (
          <img
            src="/google.png"
            alt=""
            className="w-4 h-4 object-contain shrink-0"
            onError={() => setImgFailed(true)}
          />
        ) : (
          <Globe className="w-4 h-4 text-muted-foreground/70 shrink-0" />
        )}
        <span>{cleanLabel}</span>
      </div>
    );
  }

  if (isWiki) {
    return (
      <div className="flex items-center gap-1.5">
        {!imgFailed ? (
          <img
            src="/wikipedia.png"
            alt=""
            className="w-4 h-4 object-contain shrink-0"
            onError={() => setImgFailed(true)}
          />
        ) : (
          <BookOpen className="w-4 h-4 text-muted-foreground/70 shrink-0" />
        )}
        <span>{cleanLabel}</span>
      </div>
    );
  }

  return <span>{cleanLabel}</span>;
}

export function ProgressTimeline({ steps, isComplete }: ProgressTimelineProps) {
  if (steps.length === 0) return null;

  return (
    <div className="my-3 rounded-2xl border border-border/70 bg-gradient-to-b from-sidebar/75 to-sidebar/45 px-3 py-3 shadow-[0_8px_24px_rgba(0,0,0,0.2)]">
      <div className="mb-2 px-1 text-[11px] tracking-[0.08em] uppercase text-muted-foreground">
        Progress
      </div>
      {steps.map((step, idx) => (
        <div key={step.id} className="relative flex items-start gap-3 px-1 py-1.5">
          {idx < steps.length - 1 && (
            <span className="absolute left-[12px] top-8 h-[calc(100%-12px)] w-px bg-border/70" />
          )}

          {step.status === "done" || (isComplete && idx === steps.length - 1) ? (
            <div className="z-10 flex items-center justify-center w-6 h-6 rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-400/30">
              <Check className="w-3.5 h-3.5" />
            </div>
          ) : (
            <div className="z-10 flex items-center justify-center w-6 h-6 text-primary rounded-full border border-primary/30 bg-primary/10">
              <Loader2 className="w-4 h-4 animate-spin" />
            </div>
          )}

          <div className="pt-0.5 min-w-0">
            <div className={`text-sm font-medium ${step.status === "done" ? "text-muted-foreground" : "text-foreground"}`}>
              <StepLabelWithLogo label={step.label} />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
