import { describe, expect, it } from "vitest";
import {
  buildChunkMap,
  lookupChunk,
  normalizeLegacy,
  sanitizeTitle,
  splitSegments,
} from "@/lib/citations";

const CHUNKS = [
  { title: "Handbook", ord: 0, score: 0.9 },
  { title: "Handbook", ord: 1, score: 0.8 },
];

describe("citations lib", () => {
  it("sanitizeTitle strips brackets and caps length", () => {
    expect(sanitizeTitle("a[b]:c\nd")).toBe("a-b-c-d");
    expect(sanitizeTitle("x".repeat(500)).length).toBeLessThanOrEqual(120);
  });

  it("normalizeLegacy keeps canonical known citations", () => {
    const out = normalizeLegacy("see [doc:Handbook:0] ok", CHUNKS);
    expect(out).toContain("[doc:Handbook:0]");
  });

  it("normalizeLegacy drops invented citations when chunk list present", () => {
    const out = normalizeLegacy("see [doc:Missing:9] ok", CHUNKS);
    expect(out).not.toContain("[doc:Missing:9]");
    expect(out).toContain("ok");
  });

  it("normalizeLegacy never touches fenced code", () => {
    const src = "```\n[doc:Missing:9]\n```";
    expect(normalizeLegacy(src, CHUNKS)).toContain("[doc:Missing:9]");
  });

  it("splitSegments splits text/cite and skips fences", () => {
    const segs = splitSegments("a [doc:Handbook:1] b");
    expect(segs.some((s) => s.t === "cite")).toBe(true);
  });

  it("buildChunkMap/lookupChunk round-trip", () => {
    const m = buildChunkMap(CHUNKS);
    expect(lookupChunk(m, "Handbook", 0)?.score).toBe(0.9);
    expect(lookupChunk(m, "Missing", 0)).toBeUndefined();
  });
});
