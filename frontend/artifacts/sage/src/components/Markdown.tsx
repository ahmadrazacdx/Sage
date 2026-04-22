import React, { Component, useEffect, useMemo, useRef, useState, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import "highlight.js/styles/github-dark.css";
import "katex/dist/katex.min.css";
import { Check, Copy } from "lucide-react";
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
const COPY_FEEDBACK_MS = 1400;

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

function escapeHtmlCell(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function tabBlockToHtmlTable(lines: string[]): string {
  if (lines.length < 2) return lines.join("\n");

  const rows = lines
    .map((line) => line.split("\t").map((cell) => escapeHtmlCell(cell.trim())))
    .filter((cells) => cells.length > 1 && cells.some((cell) => cell.length > 0));

  if (rows.length < 2) return lines.join("\n");

  const header = rows[0];
  const bodyRows = rows.slice(1);
  const thead = `<thead><tr>${header.map((cell) => `<th>${cell || "&nbsp;"}</th>`).join("")}</tr></thead>`;
  const tbody = `<tbody>${bodyRows
    .map((cells) => `<tr>${cells.map((cell) => `<td>${cell || "&nbsp;"}</td>`).join("")}</tr>`)
    .join("")}</tbody>`;

  return `<table>${thead}${tbody}</table>`;
}

function normalizePlannerTables(content: string): string {
  const lines = content.split("\n");
  const output: string[] = [];
  let i = 0;
  let inCodeFence = false;

  while (i < lines.length) {
    const current = lines[i] ?? "";
    if (/^\s*```/.test(current)) {
      inCodeFence = !inCodeFence;
      output.push(current);
      i += 1;
      continue;
    }

    if (!inCodeFence && current.includes("\t")) {
      const block: string[] = [];
      while (i < lines.length && (lines[i] ?? "").includes("\t")) {
        block.push(lines[i] ?? "");
        i += 1;
      }
      output.push(tabBlockToHtmlTable(block));
      continue;
    }

    output.push(current);
    i += 1;
  }

  return output.join("\n");
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

async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;

  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to legacy copy path.
    }
  }

  if (typeof document === "undefined") return false;

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();

  let copied = false;
  try {
    copied = document.execCommand("copy");
  } finally {
    document.body.removeChild(textarea);
  }

  return copied;
}

function languageLabel(language: string): string {
  if (!language) return "code";
  if (language === "plaintext") return "text";
  return language;
}

interface CodeBlockProps {
  language: string;
  className?: string;
  codeText: string;
  children: ReactNode;
}

const CodeBlock = ({ language, className, codeText, children }: CodeBlockProps) => {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, []);

  const handleCopy = async () => {
    const ok = await copyToClipboard(codeText);
    if (!ok) return;

    setCopied(true);
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
    }
    timerRef.current = window.setTimeout(() => {
      setCopied(false);
      timerRef.current = null;
    }, COPY_FEEDBACK_MS);
  };

  return (
    <div className="my-5 overflow-hidden rounded-xl border border-border/80 bg-[#111317]">
      <div className="flex items-center justify-between border-b border-border/70 bg-[#1b1f27] px-3 py-2">
        <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
          {languageLabel(language)}
        </span>
        <button
          type="button"
          onClick={handleCopy}
          className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-white/[0.03] px-2.5 py-1 text-xs font-medium text-foreground/90 transition-colors hover:bg-white/[0.08]"
          aria-label="Copy code"
        >
          {copied ? (
            <>
              <Check className="h-3.5 w-3.5" />
              Copied
            </>
          ) : (
            <>
              <Copy className="h-3.5 w-3.5" />
              Copy
            </>
          )}
        </button>
      </div>

      <pre className="!m-0 !max-w-none !overflow-x-auto !rounded-none !border-0 !bg-transparent px-4 py-3">
        <code className={className}>{children}</code>
      </pre>
    </div>
  );
};

function flattenText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map(flattenText).join("");
  }
  if (React.isValidElement(node)) {
    const childProps = node.props as { children?: ReactNode };
    return flattenText(childProps.children ?? "");
  }
  return "";
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

const MARKDOWN_SANITIZE_SCHEMA = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames || []), "details", "summary"],
  attributes: {
    ...(defaultSchema.attributes || {}),
    details: ["open"],
    summary: [],
  },
};

export const Markdown = ({ content, className, enableMermaid = true }: MarkdownProps) => {
  const normalizedContent = useMemo(() => normalizePlannerTables(content), [content]);
  const segments = useMemo(() => splitMarkdownAndSvgSegments(normalizedContent), [normalizedContent]);

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
            rehypePlugins={[rehypeRaw, [rehypeSanitize, MARKDOWN_SANITIZE_SCHEMA], rehypeKatex, rehypeHighlight] as any}
            components={{
              pre({ children }: any) {
                // Avoid react-markdown's default outer <pre> so custom blocks are not double wrapped.
                return <>{children}</>;
              },
              code({ inline, className: codeClassName, children, ...props }: any) {
                const match = /language-([A-Za-z0-9_+-]+)/.exec(codeClassName || "");
                const language = match ? match[1] : "";
                const codeString = flattenText(children).replace(/\n$/, "");
                const isInlineCode =
                  inline === true ||
                  (!language && !/\n/.test(codeString) && codeString.trim().length > 0);

                if (isInlineCode) {
                  return (
                    <code className={codeClassName} {...props}>
                      {children}
                    </code>
                  );
                }

                if (language === "mermaid" && enableMermaid) {
                  return (
                    <MermaidErrorBoundary code={codeString}>
                      <MermaidRender code={codeString} />
                    </MermaidErrorBoundary>
                  );
                }

                return (
                  <CodeBlock language={language} className={codeClassName} codeText={codeString}>
                    {children}
                  </CodeBlock>
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
              table({ className: tableClassName, children, ...props }: any) {
                return (
                  <div className="my-2 w-full overflow-x-auto">
                    <table
                      className={cn("w-full min-w-max border-collapse", tableClassName)}
                      {...props}
                    >
                      {children}
                    </table>
                  </div>
                );
              },
              th({ className: thClassName, children, ...props }: any) {
                return (
                  <th
                    className={cn(
                      "px-2 py-1.5 text-left font-semibold",
                      thClassName,
                    )}
                    {...props}
                  >
                    {children}
                  </th>
                );
              },
              td({ className: tdClassName, children, ...props }: any) {
                return (
                  <td
                    className={cn("px-2 py-1.5 align-top", tdClassName)}
                    {...props}
                  >
                    {children}
                  </td>
                );
              },
              details({ className: detailsClassName, children, ...props }: any) {
                return (
                  <details
                    className={cn(
                      "my-2",
                      detailsClassName,
                    )}
                    {...props}
                  >
                    {children}
                  </details>
                );
              },
              summary({ className: summaryClassName, children, ...props }: any) {
                return (
                  <summary
                    className={cn("cursor-pointer font-semibold", summaryClassName)}
                    {...props}
                  >
                    {children}
                  </summary>
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
