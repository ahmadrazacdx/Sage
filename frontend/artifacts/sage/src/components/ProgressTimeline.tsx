import React from "react";
import { Check, CircleDashed, Loader2 } from "lucide-react";
import { StreamStep } from "@/hooks/use-chat-stream";

interface ProgressTimelineProps {
  steps: StreamStep[];
  isComplete: boolean;
}

export function ProgressTimeline({ steps, isComplete }: ProgressTimelineProps) {
  if (steps.length === 0) return null;

  if (isComplete) {
    return (
      <div className="flex items-center gap-2 mb-4 px-3 py-2 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-lg text-sm w-fit font-medium">
        <Check className="w-4 h-4" />
        Process Complete
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 my-4 p-4 bg-[#1a1b26]/50 border border-border rounded-xl">
      {steps.map((step, idx) => (
        <div key={step.id} className="flex items-center gap-3">
          {step.status === "done" ? (
            <div className="flex items-center justify-center w-6 h-6 rounded-full bg-emerald-500/20 text-emerald-400">
              <Check className="w-3.5 h-3.5" />
            </div>
          ) : (
            <div className="flex items-center justify-center w-6 h-6 text-primary">
              <Loader2 className="w-4 h-4 animate-spin" />
            </div>
          )}
          <span className={`text-sm font-medium ${step.status === "done" ? "text-muted-foreground" : "text-foreground"}`}>
            {step.label}
          </span>
        </div>
      ))}
    </div>
  );
}
