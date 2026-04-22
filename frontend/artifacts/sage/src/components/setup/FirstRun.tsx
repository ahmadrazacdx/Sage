import React, { useState } from "react";
import { GraduationCap, ArrowRight } from "lucide-react";
import { motion } from "framer-motion";

interface FirstRunProps {
  onComplete: (name: string) => void;
}

export function FirstRun({ onComplete }: FirstRunProps) {
  const [name, setName] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (name.trim()) {
      localStorage.setItem("sage_user_name", name.trim());
      onComplete(name.trim());
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background p-4">
      <motion.div 
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: "easeOut" }}
        className="w-full max-w-md p-8 rounded-3xl bg-card border border-border shadow-2xl"
      >
        <div className="flex justify-center mb-6">
          <div className="w-16 h-16 rounded-2xl bg-primary/10 flex items-center justify-center border border-primary/20">
            <GraduationCap className="w-8 h-8 text-primary" />
          </div>
        </div>
        
        <h1 className="text-3xl font-bold text-center mb-2 text-foreground">Welcome to Sage</h1>
        <p className="text-center text-muted-foreground mb-8">Your Offline AI Study Assistant.</p>
        
        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <label htmlFor="name" className="block text-sm font-medium text-foreground mb-2">
              What should I call you?
            </label>
            <input
              id="name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Alex"
              autoFocus
              className="w-full px-4 py-3 rounded-xl bg-input border-2 border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary focus:ring-4 focus:ring-primary/10 transition-all duration-200"
            />
          </div>
          
          <button
            type="submit"
            disabled={!name.trim()}
            className="w-full flex items-center justify-center gap-2 px-6 py-3.5 rounded-xl font-semibold bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed transition-opacity shadow-lg shadow-primary/20"
          >
            Get Started
            <ArrowRight className="w-4 h-4" />
          </button>
        </form>
      </motion.div>
    </div>
  );
}
