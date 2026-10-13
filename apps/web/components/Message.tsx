"use client";

import { Children, isValidElement, memo, useMemo, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { highlight } from "sugar-high";
import type { SourceChunk } from "@/lib/store";
import { safeHref } from "@/lib/safe-url";
import {
  CITATION_RE,
  buildChunkMap,
  lookupChunk,
  normalizeLegacy,
} from "@/lib/citations";

// Re-export for Inspector/tests (single source of truth lives in lib/citations.ts).
export { splitSegments } from "@/lib/citations";
export type { Seg } from "@/lib/citations";

function CodeBlock({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const html = useMemo(() => {
    try {
      return highlight(code);
    } catch {
      return null;
    }
  }, [code]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* clipboard unavailable */ }
  };
  return (
    <div className="codeblock group my-2 overflow-hidden rounded-lg border" style={{ borderColor: "var(--border)", background: "var(--code-bg)" }}>
      <div className="flex items-center justify-between px-3 py-1.5 text-[11px]" style={{ borderBottom: "1px solid var(--border)", color: "var(--ink-faint)" }}>
        <span className="font-mono uppercase tracking-wide">{lang}</span>
        <button onClick={copy} className="rounded px-1.5 py-0.5 transition-colors hover:text-white" title="Copy code">
          {copied ? "Copied ✓" : "Copy"}
        </button>
      </div>
      <pre className="slim-scroll overflow-x-auto p-3 text-[12.5px] leading-relaxed text-slate-200">
        {html
          ? <code dangerouslySetInnerHTML={{ __html: html }} />
          : <code>{code}</code>}
      </pre>
    </div>
  );
}

function CiteBadge({ title, ord, chunk, onOpen }: {
  title: string; ord: number; chunk: SourceChunk | undefined; onOpen: (c: SourceChunk) => void;
}) {
  if (!chunk) {
    // Unresolved citation: muted non-interactive label (never raw [doc:...] markup).
    return (
      <span
        title={`Source not in current results: ${title} [${ord}]`}
        className="mx-0.5 inline-flex items-center whitespace-nowrap rounded-md border px-1.5 py-px text-[0.75em] font-medium opacity-60"
        style={{ borderColor: "var(--border)" }}
      >
        <span aria-hidden>📄</span> {title} [{ord}]
      </span>
    );
  }
  return (
    <button
      onClick={() => onOpen(chunk)}
      title={`Open source: ${chunk.title} (score ${chunk.score.toFixed(2)})`}
      className="mx-0.5 inline-flex items-center gap-1 whitespace-nowrap rounded-md border border-kroma-500/40 bg-kroma-500/10 px-1.5 py-px text-[0.75em] font-semibold text-kroma-600 transition-all hover:bg-kroma-500/25 dark:text-kroma-300"
    >
      <span aria-hidden>📄</span> {title} [{ord}]
      <span className="font-normal opacity-70">{chunk.score.toFixed(2)}</span>
    </button>
  );
}

/** Split a plain string into text + CiteBadge nodes. */
function inlineCites(
  text: string,
  byKey: Map<string, SourceChunk>,
  onOpen: (c: SourceChunk) => void,
  keyBase: string
): ReactNode[] {
  const out: ReactNode[] = [];
  CITATION_RE.lastIndex = 0;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = CITATION_RE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const title = m[1];
    const ord = Number(m[2]);
    out.push(
      <CiteBadge
        key={`${keyBase}-${k++}`}
        title={title}
        ord={ord}
        chunk={lookupChunk(byKey, title, ord)}
        onOpen={onOpen}
      />
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** Apply inlineCites to direct string children; leave elements (strong/em/a) to their own handlers. */
function withCites(
  children: ReactNode,
  byKey: Map<string, SourceChunk>,
  onOpen: (c: SourceChunk) => void
): ReactNode {
  return Children.toArray(children).flatMap((child, idx) => {
    if (typeof child === "string") return inlineCites(child, byKey, onOpen, `c${idx}`);
    return [child];
  });
}

function makeMdComponents(
  byKey: Map<string, SourceChunk>,
  onOpen: (c: SourceChunk) => void
): Components {
  const textWrapper = (props: { children?: ReactNode }) => <>{withCites(props.children, byKey, onOpen)}</>;
  return {
    pre(props) {
      const child = Children.toArray(props.children)[0];
      if (isValidElement<{ className?: string; children?: React.ReactNode }>(child) && child.type === "code") {
        const cls = child.props.className ?? "";
        const lang = /language-([\w+-]+)/.exec(cls)?.[1] ?? "text";
        const code = String(child.props.children ?? "").replace(/\n$/, "");
        return <CodeBlock lang={lang} code={code} />;
      }
      return <pre>{props.children}</pre>;
    },
    // Inline + fenced code must never parse citations.
    code(props) {
      return <code className={props.className}>{props.children}</code>;
    },
    a(props) {
      // safe-url allowlist: http/https/mailto/# + relative. Block javascript:/data:/vbscript:.
      const safe = safeHref(props.href);
      if (!safe) return <span>{withCites(props.children, byKey, onOpen)}</span>;
      return <a href={safe} target="_blank" rel="noopener noreferrer">{withCites(props.children, byKey, onOpen)}</a>;
    },
    p(props) {
      return <p>{withCites(props.children, byKey, onOpen)}</p>;
    },
    li(props) {
      return <li>{withCites(props.children, byKey, onOpen)}</li>;
    },
    h1: textWrapper as Components["h1"],
    h2: textWrapper as Components["h2"],
    h3: textWrapper as Components["h3"],
    strong(props) {
      return <strong>{withCites(props.children, byKey, onOpen)}</strong>;
    },
    em(props) {
      return <em>{withCites(props.children, byKey, onOpen)}</em>;
    },
    blockquote(props) {
      return <blockquote>{withCites(props.children, byKey, onOpen)}</blockquote>;
    },
    td(props) {
      return <td>{withCites(props.children, byKey, onOpen)}</td>;
    },
    th(props) {
      return <th>{withCites(props.children, byKey, onOpen)}</th>;
    },
  };
}

export const MessageBody = memo(function MessageBody({ content, chunks, onOpenSource }: {
  content: string; chunks?: SourceChunk[]; onOpenSource: (c: SourceChunk) => void;
}) {
  const normalized = useMemo(() => normalizeLegacy(content, chunks), [content, chunks]);
  const byKey = useMemo(() => buildChunkMap(chunks ?? []), [chunks]);
  const components = useMemo(() => makeMdComponents(byKey, onOpenSource), [byKey, onOpenSource]);
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {normalized}
      </ReactMarkdown>
    </div>
  );
});
