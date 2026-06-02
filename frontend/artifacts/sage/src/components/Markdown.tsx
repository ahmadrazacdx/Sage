import React, { Component, useEffect, useMemo, useRef, useState, useCallback, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import "highlight.js/styles/github-dark.css";
import "katex/dist/katex.min.css";
import { Check, Copy, Download, AlertCircle } from "lucide-react";
import { v4 as uuidv4 } from "uuid";
import { cn } from "@/lib/utils";
const SVG_BLOCK_REGEX = /<svg[\s\S]*?<\/svg>/gi;
const LEGACY_SVG_WRAPPER_REGEX = /<div[^>]*class=["']sage-diagram-svg["'][^>]*>([\s\S]*?)<\/div>/gi;
const DANGEROUS_SVG_TAGS = new Set([
  "script",
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

const SandboxedSvg = React.memo(({ svg, title }: { svg: string; title: string }) => {
  const sourceDoc = useMemo(() => {
    const SUBGRAPH_COLORS = [
      { bg: "rgba(30,64,175,0.15)", border: "#1e40af" },  // Blue
      { bg: "rgba(91,33,182,0.15)", border: "#5b21b6" },  // Violet
      { bg: "rgba(6,95,70,0.15)", border: "#065f46" },    // Emerald
      { bg: "rgba(146,64,14,0.15)", border: "#92400e" },   // Amber
      { bg: "rgba(157,23,77,0.15)", border: "#9d174d" },   // Pink
      { bg: "rgba(30,41,59,0.15)", border: "#1e293b" },    // Slate
    ];

    const clusterRules = SUBGRAPH_COLORS.map((c, i) =>
      `g.cluster:nth-of-type(${i + 1}) > rect, g.cluster:nth-of-type(${i + 1}) rect.cluster-box { fill: ${c.bg} !important; stroke: ${c.border} !important; }`
    ).join("\n");

    return [
      "<!doctype html>",
      '<html lang="en">',
      "<head>",
      '<meta charset="utf-8" />',
      '<meta name="viewport" content="width=device-width, initial-scale=1" />',
      "<style>",
      "html,body{margin:0;padding:0;background:#0f172a;color:#e2e8f0;height:100%;box-sizing:border-box;}",
      "body{display:flex;flex-direction:column;align-items:center;justify-content:flex-start;overflow:auto;padding:40px 20px;}",
      "svg{max-width:95%;height:auto;display:block;font-family:'Comic Sans MS', 'Comic Sans', cursive !important;overflow:visible !important;flex-shrink:0;}",
      ".node rect, .node polygon, .node circle, .node ellipse{rx:8 !important;ry:8 !important;}",
      ".cluster > rect, .cluster rect.cluster-box{rx:16 !important;ry:16 !important;stroke-dasharray:8 4 !important;stroke-width:2.5px !important;}",
      ".node rect[style], .node polygon[style], .node circle[style], .node ellipse[style]{rx:8 !important;ry:8 !important;}",
      clusterRules,
      ".label, .edgeLabel, .nodeLabel{font-family:'Comic Sans MS', 'Comic Sans', cursive;font-size:14px;fill:#e2e8f0 !important;color:#e2e8f0 !important;}",
      ".label foreignObject, .node foreignObject{font-family:'Comic Sans MS', 'Comic Sans', cursive !important;color:#e2e8f0 !important;}",
      ".katex{font-family:'Latin Modern', 'Computer Modern', 'CMU Serif', serif !important;color:#e2e8f0 !important;}",
      ".cluster-label foreignObject{overflow:visible !important;}",
      ".cluster-label text, .cluster-label span, .cluster-label div{font-family:'Comic Sans MS', 'Comic Sans', cursive !important;font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;opacity:0.9;fill:#e2e8f0 !important;color:#e2e8f0 !important;white-space:nowrap !important;width:max-content !important;min-width:100% !important;text-shadow:0 1px 2px rgba(0,0,0,0.3);}",
      ".edgeLabel, .edgeLabel span{background:transparent !important;padding:2px 4px;color:#e2e8f0 !important;fill:#e2e8f0 !important;}",
      "</style>",
      "</head>",
      "<body>",
      svg,
      "</body>",
      "</html>",
    ].join("\n");
  }, [svg]);

  const handleDownload = useCallback(() => {
    const styles = [
      "svg{font-family:'Inter', sans-serif;}",
      ".node rect, .node polygon, .node circle, .node ellipse{rx:12px;ry:12px;}",
      ".cluster > rect, .cluster rect.cluster-box{rx:24px;ry:24px;stroke-dasharray:8 4;stroke-width:2.5px;}",
      ".label, .edgeLabel, .nodeLabel{font-family:'Inter', sans-serif;font-size:13px;fill:#e2e8f0;}",
      "text{fill:#e2e8f0;}",
    ].join("");

    const styledSvg = svg.replace(">", `><style>${styles}</style>`);
    const svgWithXmlns = styledSvg.includes("xmlns")
      ? styledSvg
      : styledSvg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"');

    const blob = new Blob([svgWithXmlns], { type: "image/svg+xml;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const slug = (title || "diagram")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_|_$/g, "")
      .slice(0, 48);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${slug}.svg`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [svg, title]);

  return (
    <div className="my-6 rounded-xl border border-border bg-[#0f172a] overflow-hidden">
      <div className="flex items-center justify-between border-b border-border/70 bg-[#1e293b] px-3 py-2">
        <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
          diagram
        </span>
        <button
          type="button"
          onClick={handleDownload}
          className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-white/[0.03] px-2.5 py-1 text-xs font-medium text-foreground/90 transition-colors hover:bg-white/[0.08]"
          aria-label="Download SVG"
        >
          <Download className="h-3.5 w-3.5" />
          Download SVG
        </button>
      </div>
      <iframe
        className="w-full h-[700px] md:h-[600px] border-0"
        sandbox=""
        referrerPolicy="no-referrer"
        loading="lazy"
        title={title}
        srcDoc={sourceDoc}
      />
    </div>
  );
});

class MermaidErrorBoundary extends Component<{ children: ReactNode, code: string }, { hasError: boolean }> {
  constructor(props: { children: ReactNode, code: string }) {
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
let elkRegistered = false;
const diagramCache: Record<string, string> = {};

function trimOrphanNodes(code: string): string {
  if (!code.trim().toLowerCase().startsWith('flowchart') && !code.trim().toLowerCase().startsWith('graph')) {
    return code;
  }
  const lines = code.split('\n');
  const edgeRegex = /--.*?-->|--.*?---|==.*?==>|-\..*?\.->|-->|---|==>|===|-\.->|-\.-/;

  const adj = new Map<string, Set<string>>();
  const addEdge = (u: string, v: string) => {
    if (!adj.has(u)) adj.set(u, new Set());
    if (!adj.has(v)) adj.set(v, new Set());
    adj.get(u)!.add(v);
    adj.get(v)!.add(u);
  };

  for (const line of lines) {
    if (edgeRegex.test(line)) {
      const parts = line.split(edgeRegex);
      let prevNodes: string[] | null = null;
      for (const part of parts) {
        let cleanPart = part.trim().replace(/^\|[^|]+\|\s*/, '');
        const ids = cleanPart.split(/\s*&\s*/);
        const currentNodes: string[] = [];
        for (const idStr of ids) {
          const idMatch = idStr.trim().match(/^([a-zA-Z0-9_]+)/);
          if (idMatch) {
            const nodeId = idMatch[1];
            currentNodes.push(nodeId);
            if (!adj.has(nodeId)) adj.set(nodeId, new Set());
          }
        }
        if (prevNodes && prevNodes.length > 0 && currentNodes.length > 0) {
          for (const u of prevNodes) {
            for (const v of currentNodes) {
              addEdge(u, v);
            }
          }
        }
        prevNodes = currentNodes;
      }
    }
  }

  if (adj.size === 0) return code;

  const subgraphIds = new Set<string>();
  for (const line of lines) {
    const m = line.match(/^\s*subgraph\s+([^\s\[\"']+|\"[^\"]+\"|'[^']+')/);
    if (m) subgraphIds.add(m[1].trim());
  }

  let largestComponent = new Set<string>();
  const visited = new Set<string>();

  for (const startNode of adj.keys()) {
    if (!visited.has(startNode)) {
      const component = new Set<string>();
      const queue = [startNode];
      visited.add(startNode);
      component.add(startNode);

      while (queue.length > 0) {
        const u = queue.shift()!;
        for (const v of adj.get(u)!) {
          if (!visited.has(v)) {
            visited.add(v);
            component.add(v);
            queue.push(v);
          }
        }
      }

      if (component.size > largestComponent.size) {
        largestComponent = component;
      }
    }
  }

  const resultLines = [];
  let edgeIndex = 0;
  let newEdgeCounter = 0;
  const newEdgeIndexMap = new Map<number, number>();

  for (const line of lines) {
    const trimmed = line.trim();

    if (trimmed.startsWith('subgraph ') || trimmed === 'end') {
      resultLines.push(line);
      continue;
    }

    if (trimmed.startsWith('class ')) {
      const match = trimmed.match(/^class\s+([^%:\n]+?)\s+([a-zA-Z0-9_]+)$/);
      if (match) {
        const nodes = match[1].split(',').map(n => n.trim());
        const validNodes = nodes.filter(n => largestComponent.has(n));
        if (validNodes.length === 0) continue;
        resultLines.push(line.replace(match[1], validNodes.join(',')));
        continue;
      }
    }

    if (trimmed.startsWith('linkStyle ')) {
      const match = trimmed.match(/^linkStyle\s+([0-9,\s]+)\s+(.+)$/);
      if (match) {
        const oldIndices = match[1].split(',').map(n => parseInt(n.trim(), 10));
        const newIndices = oldIndices.map(i => newEdgeIndexMap.get(i)).filter(i => i !== undefined);
        if (newIndices.length === 0) continue;
        resultLines.push(`    linkStyle ${newIndices.join(',')} ${match[2]}`);
        continue;
      }
    }

    if (trimmed.startsWith('style ')) {
      const match = trimmed.match(/^style\s+([^\s\[\"']+|\"[^\"]+\"|'[^']+')\s+/);
      if (match) {
        const target = match[1].trim();
        if (!largestComponent.has(target) && !subgraphIds.has(target)) continue;
      }
    }

    const isSpecial = trimmed.startsWith('classDef') ||
      trimmed.startsWith('%%') ||
      trimmed.startsWith('note ');

    if (!isSpecial && edgeRegex.test(trimmed)) {
      let keepEdge = true;
      const parts = trimmed.split(edgeRegex);
      for (const part of parts) {
        let cleanPart = part.trim().replace(/^\|[^|]+\|\s*/, '');
        const ids = cleanPart.split(/\s*&\s*/);
        for (const idStr of ids) {
          const idMatch = idStr.trim().match(/^([a-zA-Z0-9_]+)/);
          if (idMatch) {
            const nodeId = idMatch[1];
            if (adj.has(nodeId) && !largestComponent.has(nodeId)) {
              keepEdge = false;
            }
          }
        }
      }

      let lineEdgeCount = 0;
      const cleanParts = parts.map(p => p.trim().replace(/^\|[^|]+\|\s*/, ''));
      for (let i = 0; i < cleanParts.length - 1; i++) {
        const leftIds = cleanParts[i].split('&').length;
        const rightIds = cleanParts[i + 1].split('&').length;
        lineEdgeCount += (leftIds * rightIds);
      }
      if (lineEdgeCount === 0) lineEdgeCount = 1;

      if (!keepEdge) {
        edgeIndex += lineEdgeCount;
        continue;
      }

      for (let i = 0; i < lineEdgeCount; i++) {
        newEdgeIndexMap.set(edgeIndex + i, newEdgeCounter + i);
      }
      edgeIndex += lineEdgeCount;
      newEdgeCounter += lineEdgeCount;

      resultLines.push(line);
      continue;
    }

    if (!isSpecial) {
      const nodeDefMatch = trimmed.match(/^([a-zA-Z0-9_]+)(?:\s*[\[\({].*[\]\)}])?$/);
      if (nodeDefMatch) {
        const nodeId = nodeDefMatch[1];
        if (!largestComponent.has(nodeId)) {
          continue;
        }
      }
    }
    resultLines.push(line);
  }

  let pass1 = resultLines.join('\n');
  let prev;
  do {
    prev = pass1;
    pass1 = pass1.replace(/subgraph\s+[^\n]+[\r\n]+(\s*[\r\n]+)*end\b/g, '');
  } while (pass1 !== prev);

  return pass1;
}

const MermaidRender = React.memo(({ code }: { code: string }) => {
  const id = useRef(`mermaid-${uuidv4().replace(/-/g, "")}`);
  const [safeSvg, setSafeSvg] = useState<string | null>(diagramCache[code] || null);
  const [error, setError] = useState<boolean>(false);
  const lastRenderedCode = useRef<string>(diagramCache[code] ? code : "");
  const errorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let isMounted = true;
    if (errorTimerRef.current !== null) {
      clearTimeout(errorTimerRef.current);
    }

    const renderDiagram = async () => {
      // Pre-check for incomplete streaming code
      if (!code || code.trim().length < 5) return;
      if (code === lastRenderedCode.current) return;

      if (diagramCache[code]) {
        if (isMounted) {
          setSafeSvg(diagramCache[code]);
          setError(false);
        }
        lastRenderedCode.current = code;
        return;
      }

      try {
        lastRenderedCode.current = code;
        if (!mermaidModule) {
          mermaidModule = await import("mermaid");
        }

        if (!elkRegistered) {
          try {
            const elkLayouts = await import("@mermaid-js/layout-elk");
            mermaidModule.default.registerLayoutLoaders(elkLayouts.default || elkLayouts);
            elkRegistered = true;
          } catch (elkErr) {
            console.warn("ELK layout engine not available, using default Dagre:", elkErr);
          }
        }

        if (!mermaidInitialized) {
          mermaidModule.default.initialize({
            startOnLoad: false,
            theme: "base",
            logLevel: "error",
            securityLevel: "strict",
            fontFamily: "'Comic Sans MS', 'Comic Sans', cursive",
            themeVariables: {
              /* Neutral/transparent defaults so classDef colors are never overridden */
              primaryColor: "transparent",
              primaryTextColor: "#e2e8f0",
              primaryBorderColor: "#475569",
              lineColor: "#64748b",
              secondaryColor: "transparent",
              tertiaryColor: "transparent",
              background: "#0f172a",
              mainBkg: "transparent",
              nodeBorder: "#475569",
              clusterBkg: "rgba(15,23,42,0.3)",
              clusterBorder: "#334155",
              titleColor: "#e2e8f0",
              edgeLabelBackground: "transparent",
              nodeTextColor: "#e2e8f0",
            },
            flowchart: {
              htmlLabels: true,
              curve: "basis",
              padding: 8,
              nodeSpacing: 40,
              rankSpacing: 50,
              defaultRenderer: "elk",
            },
            elk: {
              edgeRouting: "SPLINES"
            }
          });
          mermaidInitialized = true;
        }

        const strippedCode = code.replace(/,rx:\s*\d+,ry:\s*\d+/g, '');
        const safeCode = trimOrphanNodes(strippedCode);

        try {
          await mermaidModule.default.parse(safeCode, { suppressErrors: true });
        } catch (parseErr) {
          if (isMounted) {
            errorTimerRef.current = setTimeout(() => {
              if (isMounted) setError(true);
            }, 2000);
          }
          return;
        }

        const { svg: renderedSvg } = await mermaidModule.default.render(id.current, safeCode);

        // Patch ELK missing classes bug
        let patchedSvg = renderedSvg;
        const classRegex = /^\s*class\s+([^%:\n]+?)\s+([a-zA-Z0-9_]+)\s*$/gm;
        let classMatch;
        const classMap = new Map<string, string>();
        while ((classMatch = classRegex.exec(safeCode)) !== null) {
          const nodes = classMatch[1].split(',').map(n => n.trim());
          const className = classMatch[2].trim();
          for (const node of nodes) {
            if (node) classMap.set(node, className);
          }
        }

        if (classMap.size > 0) {
          try {
            const parser = new DOMParser();
            const doc = parser.parseFromString(patchedSvg, "image/svg+xml");
            const svgNodes = doc.querySelectorAll('.node');
            svgNodes.forEach(node => {
              const idAttr = node.getAttribute('id') || '';
              for (const [nodeId, className] of classMap.entries()) {
                // Mermaid IDs typically look like: flowchart-nodeId-123 or just nodeId
                const idParts = idAttr.split('-');
                if (idParts.includes(nodeId) || idAttr === nodeId || idAttr.includes(`-${nodeId}-`)) {
                  node.classList.add(className);
                }
              }
            });
            const serializer = new XMLSerializer();
            patchedSvg = serializer.serializeToString(doc);
          } catch (patchErr) {
            console.warn("Failed to patch ELK classes:", patchErr);
          }
        }

        const sanitized = sanitizeSvgMarkup(patchedSvg);
        if (isMounted) {
          if (!sanitized) {
            setSafeSvg(null);
            errorTimerRef.current = setTimeout(() => {
              if (isMounted) setError(true);
            }, 2000);
          } else {
            diagramCache[code] = sanitized;
            setSafeSvg(sanitized);
            setError(false);
          }
        }
      } catch (err) {
        console.warn("Mermaid render failed:", err);
        if (isMounted) {
          errorTimerRef.current = setTimeout(() => {
            if (isMounted) setError(true);
          }, 2000);
        }
      }
    };

    const debounceTimer = setTimeout(() => {
      renderDiagram();
    }, 400);

    return () => {
      isMounted = false;
      clearTimeout(debounceTimer);
      if (errorTimerRef.current !== null) {
        clearTimeout(errorTimerRef.current);
      }
    };
  }, [code]);

  const handleDownloadCode = () => {
    const blob = new Blob([code], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `diagram_source.mmd`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  if (error) {
    return (
      <div className="my-6 rounded-xl border border-red-500/30 bg-[#0f172a] overflow-hidden">
        <div className="flex items-center justify-between border-b border-red-500/20 bg-red-500/5 px-3 py-2">
          <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-red-300/80">
            Diagram Syntax Error
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                navigator.clipboard.writeText(code);
              }}
              className="inline-flex items-center gap-1.5 rounded-md border border-red-500/20 bg-red-500/10 px-2.5 py-1 text-xs font-medium text-red-200 transition-colors hover:bg-red-500/20"
            >
              <Copy className="h-3.5 w-3.5" />
              Copy Source
            </button>
            <button
              type="button"
              onClick={handleDownloadCode}
              className="inline-flex items-center gap-1.5 rounded-md border border-red-500/20 bg-red-500/10 px-2.5 py-1 text-xs font-medium text-red-200 transition-colors hover:bg-red-500/20"
            >
              <Download className="h-3.5 w-3.5" />
              Download Mermaid Source
            </button>
          </div>
        </div>
        <div className="p-4 text-red-300/90 text-sm font-mono whitespace-pre-wrap overflow-x-auto">
          <div className="flex items-center gap-2 mb-2 text-red-400 font-bold">
            <AlertCircle className="h-4 w-4" />
            <span>Browser rendering failed</span>
          </div>
          <p className="mb-4 opacity-80">This diagram contains syntax that the browser-side Mermaid parser could not process. You can download the source code to render it in an external editor.</p>
          <details className="mt-2 text-xs opacity-70 border-t border-red-500/10 pt-2">
            <summary className="cursor-pointer hover:text-red-200 transition-colors">View Source Code</summary>
            <pre className="mt-2 p-2 bg-black/40 rounded border border-white/5">{code}</pre>
          </details>
        </div>
      </div>
    );
  }

  if (!safeSvg) {
    return <div className="text-muted-foreground text-sm py-4 animate-pulse">Rendering diagram...</div>;
  }

  return <SandboxedSvg svg={safeSvg} title="Mermaid diagram" />;
});

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
        const segmentKey = `segment-${segment.type}-${index}-${segment.content.length}`;
        if (segment.type === "svg") {
          const sanitized = sanitizeSvgMarkup(segment.content);
          if (!sanitized) {
            return (
              <div
                key={segmentKey}
                className="p-4 bg-red-500/10 text-red-300 border border-red-500/20 rounded-md text-sm my-4 font-mono"
              >
                ⚠️ Unsafe or invalid SVG was blocked.
              </div>
            );
          }
          return <SandboxedSvg key={segmentKey} svg={sanitized} title="Generated diagram" />;
        }

        return (
          <ReactMarkdown
            key={segmentKey}
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
                  // Use codeString hash/length for stable key inside ReactMarkdown
                  return (
                    <MermaidErrorBoundary key={`mermaid-${codeString.length}`} code={codeString}>
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
