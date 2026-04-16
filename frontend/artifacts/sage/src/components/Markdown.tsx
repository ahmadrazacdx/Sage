import React, { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github-dark.css";
import { v4 as uuidv4 } from "uuid";
import { cn } from "@/lib/utils";

let mermaidModule: Awaited<ReturnType<typeof import("mermaid")>> | null = null;
let mermaidInitialized = false;

const MermaidRender = ({ code }: { code: string }) => {
  const id = useRef(`mermaid-${uuidv4()}`);
  const containerRef = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState<string>("");
  const [error, setError] = useState<boolean>(false);

  useEffect(() => {
    let isMounted = true;
    const renderDiagram = async () => {
      try {
        if (!mermaidModule) {
          mermaidModule = await import("mermaid");
        }

        if (!mermaidInitialized) {
          mermaidModule.default.initialize({
            startOnLoad: false,
            theme: "dark",
            securityLevel: "loose",
            fontFamily: "Inter, sans-serif",
          });
          mermaidInitialized = true;
        }

        const { svg: renderedSvg } = await mermaidModule.default.render(id.current, code);
        if (isMounted) {
          setSvg(renderedSvg);
          setError(false);
        }
      } catch (err) {
        console.error("Mermaid error:", err);
        if (isMounted) setError(true);
      }
    };
    renderDiagram();
    return () => { isMounted = false; };
  }, [code]);

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 text-red-400 border border-red-500/20 rounded-md text-sm my-4 font-mono">
        Error rendering diagram
      </div>
    );
  }

  return (
    <div 
      ref={containerRef} 
      className="flex justify-center my-6 p-4 bg-[#1a1b26] rounded-xl border border-border overflow-x-auto"
      dangerouslySetInnerHTML={{ __html: svg }} 
    />
  );
};

interface MarkdownProps {
  content: string;
  className?: string;
  enableMermaid?: boolean;
}

export const Markdown = ({ content, className, enableMermaid = true }: MarkdownProps) => {

  return (
    <div className={cn("prose prose-invert max-w-none break-words", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          code({ node, className: codeClassName, children, ...props }) {
            const match = /language-(\w+)/.exec(codeClassName || "");
            const language = match ? match[1] : "";

            if (language === "mermaid" && enableMermaid) {
              return <MermaidRender code={String(children).replace(/\n$/, "")} />;
            }

            return (
              <code className={codeClassName} {...props}>
                {children}
              </code>
            );
          },
          a({ node, className: aClassName, children, href, ...props }) {
            return (
              <a href={href} target="_blank" rel="noopener noreferrer" className={cn("text-primary hover:underline", aClassName)} {...props}>
                {children}
              </a>
            );
          }
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
};
