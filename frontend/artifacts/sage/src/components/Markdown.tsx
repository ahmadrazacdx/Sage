import React, { Component, useEffect, useMemo, useRef, useState, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import "highlight.js/styles/github-dark.css";
import "katex/dist/katex.min.css";
import { v4 as uuidv4 } from "uuid";
import { cn } from "@/lib/utils";

const SVG_BLOCK_REGEX = /<svg[\s\S]*?<\/svg>/gi;
const LEGACY_SVG_WRAPPER_REGEX = /<div[^>]*class=["']sage-diagram-svg["'][^>]*>([\s\S]*?)<\/div>/gi;
const DANGEROUS_SVG_TAGS = new Set([
  "script",
  "foreignobject",
  "iframe",
  "object",
  "embed",
  "audio",
  "video",
  "canvas",
  "link",
  "meta",
]);
const URL_ATTRS = new Set(["href", "xlink:href", "src"]);
const MAX_SVG_CHARS = 500_000;

type MarkdownSegment =
  | { type: "markdown"; content: string }
  | { type: "svg"; content: string };

function isSafeUrl(url: string): boolean {
  const value = url.trim().toLowerCase();
  if (!value) return true;
  if (value.startsWith("#")) return true;
  if (value.startsWith("/")) return true;
  if (value.startsWith("./") || value.startsWith("../")) return true;
  if (value.startsWith("http://") || value.startsWith("https://")) return true;
  if (value.startsWith("data:image/")) return true;
  return false;
}

function normalizeLegacySvgWrappers(content: string): string {
  return content.replace(LEGACY_SVG_WRAPPER_REGEX, "$1");
}

export function splitMarkdownAndSvgSegments(content: string): MarkdownSegment[] {
  const normalized = normalizeLegacySvgWrappers(content);
  const segments: MarkdownSegment[] = [];
  let lastIndex = 0;

  for (const match of normalized.matchAll(SVG_BLOCK_REGEX)) {
    const svg = match[0];
    const index = match.index ?? 0;

    if (index > lastIndex) {
      const before = normalized.slice(lastIndex, index);
      if (before.trim() || before.includes("\n")) {
        segments.push({ type: "markdown", content: before });
      }
    }

    segments.push({ type: "svg", content: svg });
    lastIndex = index + svg.length;
  }

  if (lastIndex < normalized.length) {
    const tail = normalized.slice(lastIndex);
    if (tail.trim() || tail.includes("\n")) {
      segments.push({ type: "markdown", content: tail });
    }
  }

  if (segments.length === 0) {
    segments.push({ type: "markdown", content: normalized });
  }

  return segments;
}

export function sanitizeSvgMarkup(rawSvg: string): string | null {
  if (!rawSvg.trim()) return null;
  if (rawSvg.length > MAX_SVG_CHARS) return null;

  const parser = new DOMParser();
  const doc = parser.parseFromString(rawSvg, "image/svg+xml");

  if (doc.querySelector("parsererror")) return null;
  const root = doc.documentElement;
  if (!root || root.tagName.toLowerCase() !== "svg") return null;

  const allElements = Array.from(root.querySelectorAll("*"));
  for (const element of allElements) {
    const tagName = element.tagName.toLowerCase();
    if (DANGEROUS_SVG_TAGS.has(tagName)) {
      element.remove();
      continue;
    }

    for (const attrName of element.getAttributeNames()) {
      const attrLower = attrName.toLowerCase();
      const value = element.getAttribute(attrName) ?? "";
      const valueLower = value.trim().toLowerCase();

      if (attrLower.startsWith("on")) {
        element.removeAttribute(attrName);
        continue;
      }

      if (URL_ATTRS.has(attrLower) && !isSafeUrl(value)) {
        element.removeAttribute(attrName);
        continue;
      }

      if (
        attrLower === "style" &&
        (valueLower.includes("javascript:") || valueLower.includes("expression("))
      ) {
        element.removeAttribute(attrName);
      }
    }
  }

  const serializer = new XMLSerializer();
  const sanitized = serializer.serializeToString(root);
  return sanitized.includes("<svg") ? sanitized : null;
}

function isSafeImageSource(src: string | undefined): boolean {
  if (!src) return false;
  return isSafeUrl(src);
}

const SandboxedSvg = ({ svg, title }: { svg: string; title: string }) => {
  const sourceDoc = useMemo(() => {
    return [
      "<!doctype html>",
      '<html lang="en">',
      "<head>",
      '<meta charset="utf-8" />',
      '<meta name="viewport" content="width=device-width, initial-scale=1" />',
      "<style>",
      "html,body{margin:0;padding:0;background:#1a1b26;color:#e5e7eb;height:100%;}",
      "body{display:flex;align-items:center;justify-content:center;overflow:auto;}",
      "svg{max-width:100%;max-height:100%;height:auto;width:auto;display:block;}",
      "</style>",
      "</head>",
      `<body>${svg}</body>`,
      "</html>",
    ].join("");
  }, [svg]);

  return (
    <div className="my-6 rounded-xl border border-border bg-[#1a1b26] overflow-hidden">
      <iframe
        className="w-full h-[360px] md:h-[420px]"
        sandbox=""
        referrerPolicy="no-referrer"
        loading="lazy"
        title={title}
        srcDoc={sourceDoc}
      />
    </div>
  );
};

class MermaidErrorBoundary extends Component<{children: ReactNode, code: string}, {hasError: boolean}> {
  constructor(props: {children: ReactNode, code: string}) {
    super(props);
    this.state = { hasError: false };
  }
  static getDerivedStateFromError() {
    return { hasError: true };
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="p-4 bg-red-500/10 text-red-300 border border-red-500/20 rounded-md text-sm my-4 font-mono flex flex-col gap-2">
          <span>⚠️ Diagram could not be rendered in browser</span>
          <details className="mt-2 text-xs opacity-70">
            <summary className="cursor-pointer">View source</summary>
            <pre className="mt-2 overflow-x-auto whitespace-pre">{this.props.code}</pre>
          </details>
        </div>
      );
    }
    return this.props.children;
  }
}

let mermaidModule: any = null;
let mermaidInitialized = false;

const MermaidRender = ({ code }: { code: string }) => {
  const id = useRef(`mermaid-${uuidv4()}`);
  const [safeSvg, setSafeSvg] = useState<string | null>(null);
  const [error, setError] = useState<boolean>(false);

  useEffect(() => {
    let isMounted = true;
    const renderDiagram = async () => {
      // Pre-check for incomplete streaming code
      if (!code || code.trim().length < 5) return;
      try {
        if (!mermaidModule) {
          mermaidModule = await import("mermaid");
        }

        if (!mermaidInitialized) {
          mermaidModule.default.initialize({
            startOnLoad: false,
            theme: "dark",
            securityLevel: "strict",
            fontFamily: "Inter, sans-serif",
          });
          mermaidInitialized = true;
        }

        const { svg: renderedSvg } = await mermaidModule.default.render(id.current, code);
        const sanitized = sanitizeSvgMarkup(renderedSvg);
        if (isMounted) {
          if (!sanitized) {
            setSafeSvg(null);
            setError(true);
          } else {
            setSafeSvg(sanitized);
            setError(false);
          }
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
      <div className="p-4 bg-red-500/10 text-red-300 border border-red-500/20 rounded-md text-sm my-4 font-mono">
        ⚠️ Diagram syntax error
      </div>
    );
  }

  if (!safeSvg) {
    return <div className="text-muted-foreground text-sm py-4 animate-pulse">Rendering diagram...</div>;
  }

  return <SandboxedSvg svg={safeSvg} title="Mermaid diagram" />;
};

interface MarkdownProps {
  content: string;
  className?: string;
  enableMermaid?: boolean;
}

export const Markdown = ({ content, className, enableMermaid = true }: MarkdownProps) => {
  const segments = useMemo(() => splitMarkdownAndSvgSegments(content), [content]);

  return (
    <div className={cn("prose prose-invert max-w-none break-words", className)}>
      {segments.map((segment, index) => {
        if (segment.type === "svg") {
          const sanitized = sanitizeSvgMarkup(segment.content);
          if (!sanitized) {
            return (
              <div
                key={`svg-${index}`}
                className="p-4 bg-red-500/10 text-red-300 border border-red-500/20 rounded-md text-sm my-4 font-mono"
              >
                ⚠️ Unsafe or invalid SVG was blocked.
              </div>
            );
          }
          return <SandboxedSvg key={`svg-${index}`} svg={sanitized} title="Generated diagram" />;
        }

        return (
          <ReactMarkdown
            key={`md-${index}`}
            remarkPlugins={[remarkGfm, remarkMath] as any}
            rehypePlugins={[rehypeKatex, rehypeHighlight] as any}
            components={{
              code({ className: codeClassName, children, ...props }: any) {
                const match = /language-(\w+)/.exec(codeClassName || "");
                const language = match ? match[1] : "";

                if (language === "mermaid" && enableMermaid) {
                  const codeString = String(children).replace(/\n$/, "");
                  return (
                    <MermaidErrorBoundary code={codeString}>
                      <MermaidRender code={codeString} />
                    </MermaidErrorBoundary>
                  );
                }

                return (
                  <code className={codeClassName} {...props}>
                    {children}
                  </code>
                );
              },
              a({ className: aClassName, children, href, ...props }: any) {
                return (
                  <a
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={cn("text-primary hover:underline", aClassName)}
                    {...props}
                  >
                    {children}
                  </a>
                );
              },
              img({ src, alt, ...props }: any) {
                if (!isSafeImageSource(src)) {
                  return (
                    <span className="text-xs text-muted-foreground">
                      [Image blocked for security]
                    </span>
                  );
                }

                return (
                  <img
                    src={src}
                    alt={alt ?? ""}
                    loading="lazy"
                    referrerPolicy="no-referrer"
                    className="max-w-full rounded-lg border border-border"
                    {...props}
                  />
                );
              },
            }}
          >
            {segment.content}
          </ReactMarkdown>
        );
      })}
    </div>
  );
};
