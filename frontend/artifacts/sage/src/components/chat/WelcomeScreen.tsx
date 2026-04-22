import React from "react";
import { motion } from "framer-motion";
import { GraduationCap } from "lucide-react";

interface WelcomeScreenProps {
  name: string;
}

export function WelcomeScreen({ name }: WelcomeScreenProps) {
  return (
    <div className="flex-1 flex flex-col items-center justify-center p-6 w-full max-w-4xl mx-auto h-full">
      <motion.div 
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="text-center max-w-2xl"
      >
        <div className="w-20 h-20 mx-auto bg-gradient-to-br from-primary/20 to-transparent rounded-3xl flex items-center justify-center border border-primary/20 mb-6 shadow-2xl shadow-primary/10">
          <GraduationCap className="w-10 h-10 text-primary" />
        </div>
        <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">
          Hello, <span className="bg-gradient-to-r from-primary to-primary/60 bg-clip-text text-transparent">{name}</span>! 👋
        </h1>
        <p className="text-xl text-muted-foreground font-medium">What would you like to study today?</p>
      </motion.div>
    </div>
  );
}
