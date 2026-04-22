import React from "react";
import { GraduationCap, Loader2 } from "lucide-react";
import { motion } from "framer-motion";

export function ModelLoading() {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm">
      <motion.div 
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        className="flex flex-col items-center justify-center p-8 rounded-3xl bg-sidebar border border-sidebar-border shadow-2xl"
      >
        <div className="relative mb-6">
          <div className="absolute inset-0 bg-primary/20 blur-xl rounded-full animate-pulse" />
          <div className="relative w-20 h-20 rounded-2xl bg-card border border-border flex items-center justify-center">
            <GraduationCap className="w-10 h-10 text-primary animate-bounce" />
          </div>
        </div>
        
        <h2 className="text-xl font-bold text-foreground mb-2">Getting ready...</h2>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="w-4 h-4 animate-spin" />
          Loading Models & Resources
        </div>
      </motion.div>
    </div>
  );
}
