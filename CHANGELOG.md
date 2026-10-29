# Changelog

All notable changes to KromaRAG are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.0] — 2026-10-29

### Added
- Hybrid retrieval (0.7 vector + 0.3 full-text) with RBAC `WHERE` filtering.
- LangGraph agent (`router → retriever → reranker → generator → guardrail → evaluator`)
  with Gemini primary + Groq fallback and dual-layer citation defense.
- Conversations + history (threads, keyset pagination, SSE replay).
- Document lifecycle (upload / status / reindex / archive / purge).
- Clerk Google-only auth, viewer-default roles, dev bypass for local dev.
- Telemetry: LangSmith traces, W3C `traceparent`, Prometheus `/metrics` (dual-gated).
- Web: Next.js 16 + React 19 chat, documents, mobile full-bleed bars, dark/light themes.
- SDD gate (`specs/` 9 specs, `npm run spec:check`), 12-job CI, Docker prod artifact.
- Portfolio surface: `LICENSE`, `SECURITY.md`, SEO metadata, `robots`/`sitemap`/`manifest`,
  `not-found`/`error`/`loading` routes.

### Notes
- Staging deploy is manual (`scripts/deploy/README.md`): Render API + Vercel web + Neon.
  Live demo URL is wired in `README.md` once staging is green (smoke + k6 soak).
