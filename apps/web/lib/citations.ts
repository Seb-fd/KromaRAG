/** Shared citation helpers — canonical format is exactly `[doc:Title:ord]` (ord = integer).
 * Mirrors backend `app/agent/guardrails.py::normalize_citations` + `graph.py::sanitize_title`.
 * Frontend is the last line of defense: convert legacy `⌗ Title:00.50` to badges,
 * drop invented `[doc:...]` / `[1]` tags so raw artifacts never render.
 */

export type SourceChunkLike = {
  title: string;
  ord: number;
  chunk_id?: string;
  doc_id?: string;
  score?: number;
};

export type Seg = { t: "text"; v: string } | { t: "cite"; title: string; ord: number; raw: string };

export const CITATION_RE = /\[doc:([^\]:\[]+):(\d+)\]/g;
const CANONICAL_WITH_DECIMAL_RE = /\[doc:([^\[\]:]+):(\d+)(?:\.\d+)?\]/g;
const MALFORMED_DOC_RE = /\[doc:[^\]]+\]/g;
const CANONICAL_STRICT_RE = /^\[doc:[^\[\]:]+:\d+\]$/;
const LEGACY_BADGE_RE = /(?:\s|^)⌗\s*([^:\n\[]+):(\d+(?:\.\d+)?)/g;
const NUMERIC_CITE_RE = /(?<![\w\]])\[(\d{1,3})\](?!\()/g;
const FENCE_SPLIT_RE = /(```[\s\S]*?(?:```|$))/g;

export function sanitizeTitle(title: string): string {
  const t = String(title ?? "")
    .replace(/[\[\]:\n\r]+/g, "-")
    .trim()
    .replace(/\s+/g, " ");
  return t.slice(0, 120) || "untitled";
}

function buildValid(chunks?: SourceChunkLike[]): Map<string, string> {
  const m = new Map<string, string>();
  for (const c of chunks ?? []) {
    const t = sanitizeTitle(c.title);
    const o = Number(c.ord);
    if (!Number.isInteger(o)) continue;
    m.set(`${t.toLowerCase()}::${o}`, t);
  }
  return m;
}

function normalizePart(part: string, valid: Map<string, string>, hasChunks: boolean): string {
  // 1. Legacy ⌗ badges → canonical when known, else strip ⌗ artifact.
  //    Preserves leading whitespace; strips decimal timestamp (:00.41 -> :0)
  //    and wraps cleanly as [doc:Title:0] BEFORE Markdown rendering.
  part = part.replace(LEGACY_BADGE_RE, (_full, rawTitle: string, rawOrd: string) => {
    const prefix = /^\s/.test(_full) ? _full[0] : "";
    const title = sanitizeTitle(rawTitle);
    const ord = parseInt(String(rawOrd).split(".")[0], 10) || 0;
    const key = `${title.toLowerCase()}::${ord}`;
    if (valid.has(key)) return `${prefix}[doc:${valid.get(key)}:${ord}]`;
    // No trusted mapping: still remove ⌗ so no raw symbol renders.
    if (hasChunks) return `${prefix}${title}`; // unknown target → readable text only
    return `${prefix}[doc:${title}:${ord}]`; // no chunk list: syntactic cleanup only
  });

  // 2. Canonical-ish with optional decimals → normalize or drop invented.
  part = part.replace(
    CANONICAL_WITH_DECIMAL_RE,
    (full, rawTitle: string, rawOrd: string) => {
      const title = sanitizeTitle(rawTitle);
      const ord = parseInt(String(rawOrd).split(".")[0], 10);
      if (!Number.isInteger(ord)) return "";
      const key = `${title.toLowerCase()}::${ord}`;
      if (!hasChunks) return `[doc:${title}:${ord}]`;
      if (valid.has(key)) return `[doc:${valid.get(key)}:${ord}]`;
      return ""; // invented → drop so no raw artifact renders
    }
  );

  // 3. Remaining [doc:...] that isn't strict-canonical (e.g. [doc:T:Ord]) → drop.
  //    Strict-shaped but unknown target → also drop when we have a chunk list.
  part = part.replace(MALFORMED_DOC_RE, (full) => {
    if (CANONICAL_STRICT_RE.test(full)) {
      if (!hasChunks) return full;
      const inner = /\[doc:([^\[\]:]+):(\d+)\]/.exec(full);
      if (inner) {
        const t = sanitizeTitle(inner[1]);
        const o = parseInt(inner[2], 10);
        if (valid.has(`${t.toLowerCase()}::${o}`)) return full;
      }
      return "";
    }
    return ""; // placeholder / malformed → strip
  });

  // 4. Numeric-only [1] (not Markdown links) → drop to enforce canonical-only.
  if (hasChunks) part = part.replace(NUMERIC_CITE_RE, "");

  return part;
}

/** Convert legacy/malformed tags to canonical `[doc:Title:ord]` (or strip if invented).
 * Never touches fenced code blocks. Pure + testable.
 */
export function normalizeLegacy(input: string, chunks?: SourceChunkLike[]): string {
  if (!input) return input;
  const valid = buildValid(chunks);
  const hasChunks = !!chunks && chunks.length > 0;
  const parts = input.split(FENCE_SPLIT_RE);
  const out: string[] = [];
  for (const p of parts) {
    if (!p) continue;
    if (p.startsWith("```")) {
      out.push(p);
      continue;
    }
    out.push(normalizePart(p, valid, hasChunks));
  }
  return out
    .join("")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n");
}

/** Split normalized markdown into text/citation segments without touching fenced code. */
export function splitSegments(input: string): Seg[] {
  const out: Seg[] = [];
  const fenceParts = input.split(FENCE_SPLIT_RE);
  for (const part of fenceParts) {
    if (!part) continue;
    if (part.startsWith("```")) {
      out.push({ t: "text", v: part });
      continue;
    }
    let last = 0;
    CITATION_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = CITATION_RE.exec(part)) !== null) {
      if (m.index > last) out.push({ t: "text", v: part.slice(last, m.index) });
      out.push({ t: "cite", title: m[1], ord: Number(m[2]), raw: m[0] });
      last = m.index + m[0].length;
    }
    if (last < part.length) out.push({ t: "text", v: part.slice(last) });
  }
  return out;
}

export function buildChunkMap<T extends SourceChunkLike>(chunks?: T[]): Map<string, T> {
  const m = new Map<string, T>();
  for (const c of chunks ?? []) m.set(`${sanitizeTitle(c.title)}::${c.ord}`, c);
  return m;
}

export function lookupChunk<T extends SourceChunkLike>(
  map: Map<string, T>,
  title: string,
  ord: number
): T | undefined {
  return map.get(`${sanitizeTitle(title)}::${ord}`);
}
