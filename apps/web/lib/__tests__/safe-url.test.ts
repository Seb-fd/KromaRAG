import { describe, expect, it } from "vitest";
import { isSafeHref, safeHref } from "@/lib/safe-url";

describe("safe-url allowlist (XSS guard)", () => {
  it("allows http/https/mailto/anchor/relative", () => {
    for (const h of [
      "https://example.com/a",
      "http://example.com/",
      "mailto:a@b.com",
      "#section",
      "/docs/x",
    ]) {
      expect(isSafeHref(h)).toBe(true);
      expect(safeHref(h)).toBe(h);
    }
  });

  it("blocks javascript:/data:/vbscript:/file:/ftp:", () => {
    for (const h of [
      "javascript:alert(1)",
      "JaVaScRiPt:alert(1)",
      "data:text/html,<script>alert(1)</script>",
      "vbscript:msgbox(1)",
      "file:///etc/passwd",
      "ftp://example.com/x",
      "",
      "   ",
    ]) {
      expect(isSafeHref(h)).toBe(false);
      expect(safeHref(h)).toBeUndefined();
    }
  });

  it("trims whitespace before checking", () => {
    expect(safeHref("  https://example.com/  ")).toBe("https://example.com/");
    expect(safeHref("  javascript:alert(1)  ")).toBeUndefined();
  });

  it("handles null/undefined", () => {
    expect(isSafeHref(null)).toBe(false);
    expect(isSafeHref(undefined)).toBe(false);
    expect(safeHref(null)).toBeUndefined();
    expect(safeHref(undefined)).toBeUndefined();
  });
});
