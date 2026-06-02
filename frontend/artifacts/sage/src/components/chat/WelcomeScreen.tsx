import React from "react";
import { motion } from "framer-motion";

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
        <img
          src="/favicon.svg"
          className="w-24 h-24 mx-auto mb-6 object-contain hover:scale-105 transition-transform duration-300 cursor-pointer"
          alt="Sage Logo"
        />
        <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">
          Hello, <span className="bg-gradient-to-r from-primary to-primary/60 bg-clip-text text-transparent">{name}</span>! 👋
        </h1>
        <p className="text-xl text-muted-foreground font-medium">What would you like to study today?</p>
      </motion.div>
    </div>
  );
}
