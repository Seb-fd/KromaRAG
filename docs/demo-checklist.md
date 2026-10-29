# Demo capture checklist (portfolio screenshots)

Live: `https://kroma-rag.vercel.app` (API `https://kromarag-api-staging.onrender.com`).
If cold, wake the API first, then run `scripts/deploy/smoke.sh` (or `.ps1`).
Capture as the debug admin.

## Shots

1. `docs/demo-chat-1280.png` — desktop chat with a cited answer + open Inspector.
2. `docs/demo-documents-1280.png` — Library card with one `indexed` doc.
3. `docs/demo-chat-388.png` — mobile full-bleed header + 56px composer.
4. Optional 30s GIF: upload → ask → cited answer streams.

## Rules

- Redact emails / JWTs / trace IDs. Use the demo corpus, never prod data.
- Light + dark not required — dark is the canonical portfolio shot.
- Keep files under ~500 KB each (`png` → compressed, `gif` ≤ 10s).
- Reference them from `README.md` Portfolio table once added.
