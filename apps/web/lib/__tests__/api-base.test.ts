import { describe, expect, it, vi } from "vitest";
import { API_BASE_FALLBACK, resolveApiBase } from "@/lib/api-base";

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
});
