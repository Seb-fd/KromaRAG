import { describe, expect, it, vi } from "vitest";
import { API_BASE_FALLBACK, normalizeApiBaseUrl, resolveApiBase } from "@/lib/api-base";

// Invisible fixtures are ALWAYS built from char codes below: literal
// invisible bytes must never live in source (editors/linters strip them
// silently, hollowing the tests).
const ZWSP = String.fromCharCode(0x200b);
const BEL = String.fromCharCode(0x07);
const BOM = String.fromCharCode(0xfeff);
const DQ = String.fromCharCode(0x22);
const SQ = String.fromCharCode(0x27);

describe("normalizeApiBaseUrl (client fetch bases)", () => {
  it("passes correct absolute URLs through (minus trailing slash)", () => {
    expect(normalizeApiBaseUrl("https://kromarag-api-staging.onrender.com")).toBe(
      "https://kromarag-api-staging.onrender.com"
    );
    expect(normalizeApiBaseUrl("https://kromarag-api-staging.onrender.com/")).toBe(
      "https://kromarag-api-staging.onrender.com"
    );
    expect(normalizeApiBaseUrl("http://127.0.0.1:8000")).toBe("http://127.0.0.1:8000");
  });

  it("repairs the reported Vercel failure: single-slash scheme stays absolute", () => {
    // Baked value was `https:/host` (one slash) -> fetch misparsed it as
    // relative and prepended window.location.origin (`/https:/host/...` 404).
    const base = normalizeApiBaseUrl("https:/kromarag-api-staging.onrender.com");
    expect(base).toBe("https://kromarag-api-staging.onrender.com");
    const url = `${base}/health`;
    expect(url).toBe("https://kromarag-api-staging.onrender.com/health");
    expect(new URL(url).host).toBe("kromarag-api-staging.onrender.com");
  });

  it("upgrades bare hosts and keeps relative bases relative", () => {
    expect(normalizeApiBaseUrl("kromarag-api.onrender.com")).toBe(
      "https://kromarag-api.onrender.com"
    );
    expect(normalizeApiBaseUrl("")).toBe("");
    expect(normalizeApiBaseUrl("   ")).toBe("");
  });

  it("falls back loudly on non-http(s) garbage", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(normalizeApiBaseUrl("ftp://x.example")).toBe(API_BASE_FALLBACK);
      expect(normalizeApiBaseUrl("javascript:alert(1)")).toBe(API_BASE_FALLBACK);
      expect(warn).toHaveBeenCalled();
    } finally {
      warn.mockRestore();
    }
  });

  it("strips edge paste corruption (quotes, BOM, trailing controls, NBSP)", () => {
    const NBSP = String.fromCharCode(0xa0);
    const good = "https://kromarag-api-staging.onrender.com";
    expect(normalizeApiBaseUrl(DQ + good + DQ)).toBe(good);
    expect(normalizeApiBaseUrl(SQ + good + SQ)).toBe(good);
    expect(normalizeApiBaseUrl(ZWSP + good + ZWSP)).toBe(good);
    expect(normalizeApiBaseUrl(BOM + good)).toBe(good);
    expect(normalizeApiBaseUrl(good + BEL)).toBe(good);
    expect(normalizeApiBaseUrl(good + NBSP)).toBe(good);
    expect(normalizeApiBaseUrl(NBSP + good + "  ")).toBe(good);
    expect(normalizeApiBaseUrl(good + "/")).toBe(good);
  });

  it("refuses interior invisible chars instead of forging a host", () => {
    // Stripping an interior ZWSP would silently route the Clerk Bearer token
    // to a novel hostname — fail closed with a warning instead.
    const NBSP = String.fromCharCode(0xa0);
    const evil = `https://kromarag-api-staging${ZWSP}onrender.com`;
    const evil2 = `https://kromarag-api-staging${NBSP}onrender.com`;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(normalizeApiBaseUrl(evil)).toBe(API_BASE_FALLBACK);
      expect(normalizeApiBaseUrl(evil2)).toBe(API_BASE_FALLBACK);
      expect(warn).toHaveBeenCalled();
    } finally {
      warn.mockRestore();
    }
  });

  it("rejection diagnostics carry length and char codes", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      normalizeApiBaseUrl("ftp://x.example");
      const msg = warn.mock.calls.map((c) => String(c[0])).join("\n");
      expect(msg).toMatch(/len=\d+/);
      expect(msg).toMatch(/codes\[[0-9a-f ]+\]/);
    } finally {
      warn.mockRestore();
    }
  });
});

describe("resolveApiBase (rewrite destinations)", () => {
  it("prefers API_URL over NEXT_PUBLIC_API_URL", () => {
    expect(
      resolveApiBase({ API_URL: "https://a.example", NEXT_PUBLIC_API_URL: "https://b.example" })
    ).toBe("https://a.example");
  });

  it("upgrades bare hosts to https and strips trailing slashes/paths", () => {
    expect(resolveApiBase({ NEXT_PUBLIC_API_URL: "kromarag-api.onrender.com" })).toBe(
      "https://kromarag-api.onrender.com"
    );
    expect(resolveApiBase({ NEXT_PUBLIC_API_URL: "https://kromarag-api.onrender.com/" })).toBe(
      "https://kromarag-api.onrender.com"
    );
    expect(resolveApiBase({ NEXT_PUBLIC_API_URL: "  http://127.0.0.1:8000/  " })).toBe(
      "http://127.0.0.1:8000"
    );
  });

  it("falls back with a warning on missing/unparseable values", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(resolveApiBase({})).toBe(API_BASE_FALLBACK);
      expect(resolveApiBase({ NEXT_PUBLIC_API_URL: "   " })).toBe(API_BASE_FALLBACK);
      expect(resolveApiBase({ NEXT_PUBLIC_API_URL: "ftp://x.example" })).toBe(API_BASE_FALLBACK);
      expect(resolveApiBase({ NEXT_PUBLIC_API_URL: "javascript:alert(1)" })).toBe(
        API_BASE_FALLBACK
      );
      expect(warn).toHaveBeenCalled();
    } finally {
      warn.mockRestore();
    }
  });

  it("every destination built from it is a valid rewrite target", () => {
    for (const env of [
      {},
      { NEXT_PUBLIC_API_URL: "bare-host.onrender.com" },
      { API_URL: "https://api.example.com/v1/" },
    ]) {
      const api = resolveApiBase(env);
      for (const dest of [`${api}/api/v1/:path*`, `${api}/healthz`]) {
        expect(dest.startsWith("/") || dest.startsWith("http://") || dest.startsWith("https://")).toBe(
          true
        );
      }
    }
  });

  it("apiBase() uses the normalized absolute URL in direct mode", async () => {
    const prevProxy = process.env.NEXT_PUBLIC_USE_PROXY;
    const prevApi = process.env.NEXT_PUBLIC_API_URL;
    try {
      const { apiBase } = await import("@/lib/api-client");
      process.env.NEXT_PUBLIC_USE_PROXY = "0";
      process.env.NEXT_PUBLIC_API_URL = "https:/kromarag-api-staging.onrender.com/";
      expect(apiBase()).toBe("https://kromarag-api-staging.onrender.com");
      delete process.env.NEXT_PUBLIC_API_URL;
      expect(apiBase()).toBe("http://127.0.0.1:8000");
      process.env.NEXT_PUBLIC_USE_PROXY = "1";
      expect(apiBase()).toBe("");
    } finally {
      if (prevProxy === undefined) delete process.env.NEXT_PUBLIC_USE_PROXY;
      else process.env.NEXT_PUBLIC_USE_PROXY = prevProxy;
      if (prevApi === undefined) delete process.env.NEXT_PUBLIC_API_URL;
      else process.env.NEXT_PUBLIC_API_URL = prevApi;
    }
  });
});
