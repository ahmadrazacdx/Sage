import React, { useState, useEffect } from "react";
import { Markdown } from "./Markdown";

interface TypewriterMarkdownProps {
  content: string;
  speed?: number; // ms per tick roughly
}

export function TypewriterMarkdown({ content, speed = 15 }: TypewriterMarkdownProps) {
  const [displayed, setDisplayed] = useState("");

  useEffect(() => {
    let i = 0;
    const len = content.length;
    // reset if content changes completely
    setDisplayed("");
    
    // Chunk size heuristic: if it's very long, type faster
    const chunkSize = len > 2000 ? 5 : len > 500 ? 2 : 1;

    const timer = setInterval(() => {
      if (i < len) {
        setDisplayed(content.substring(0, i + chunkSize));
        i += chunkSize;
      } else {
        clearInterval(timer);
        setDisplayed(content);
      }
    }, speed);

    return () => clearInterval(timer);
  }, [content, speed]);

  return <Markdown content={displayed} />;
}
