import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const SAGE_MODES = [
  { id: "general", name: "General", icon: "🤖", placeholder: "Ask Sage anything..." },
  { id: "explain", name: "Explain", icon: "💬", placeholder: "Ask Sage to explain..." },
  { id: "thinking", name: "Thinking", icon: "🧠", placeholder: "Ask Sage to think through..." },
  { id: "quiz", name: "Quiz Me", icon: "🧩", placeholder: "Ask Sage to quiz you on..." },
  { id: "diagram", name: "Diagram", icon: "📊", placeholder: "Ask Sage to diagram..." },
  { id: "roadmap", name: "Study Plan", icon: "📅", placeholder: "Ask Sage to plan your study..." },
  { id: "fix", name: "Fix Code", icon: "🔧", placeholder: "Paste code to fix..." },
  { id: "research", name: "Research", icon: "🔬", placeholder: "Ask Sage to research..." },
] as const;

export type SageMode = typeof SAGE_MODES[number]["id"];

export const TOOL_NAMES: Record<string, string> = {
  // Node labels
  "retrieval": "📚 Retrieving course materials…",
  "reasoning": "🧠 Reasoning…",
  "response_generator": "✍️ Formatting response…",
  "quiz": "🧩 Generating quiz…",
  "diagram": "📊 Building diagram…",
  "planner": "📅 Building study plan…",
  "research": "🔬 Researching topic…",
  "code_fix": "🔧 Analysing code…",
  "general": "💬 Generating answer…",
  // Tool labels
  "corpus_search": "🔍 Searching course materials…",
  "validate_mermaid": "🔍 Validating diagram syntax…",
  "render_mermaid_svg": "🖼️ Rendering diagram SVG…",
  "search_arxiv": "📄 Searching arXiv…",
  "search_web": "🌐 Searching the web…",
  "search_wikipedia": "📖 Searching Wikipedia…",
  "calculator": "🔢 Running calculation…",
  "execute_python": "⚙️ Executing code…",
  "export_pdf": "📋 Generating PDF report…",
  "export_markdown": "📝 Saving markdown…"
};
