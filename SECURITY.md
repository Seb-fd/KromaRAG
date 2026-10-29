# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

Do not open a public issue for security reports.
Email the maintainer via the GitHub profile with:

- Affected component (`apps/api` / `apps/web` / deploy scripts)
- Steps to reproduce, scope, and impact
- Any logs with secrets **redacted** (DSN/JWT/API keys)

Expect a response within 72 hours. Fixes ship as patch releases with a
`CHANGELOG.md` entry; reporters are credited unless anonymity is requested.

## Secrets Hygiene

- Never commit `.env`, `.env.local`, `apps/api/.env`, `apps/web/.env.local`,
  or `debug-credentials.local.md` (all gitignored, pre-commit + pre-push guarded).
- Prod secrets live only in Cloud Run Secret Manager / Vercel env / Render dashboard.
- Clerk: `kromarag-dev` for local/staging, `kromarag-prod` for prod — never share keys.
- `AUTH_SECRET` is dev-only. Prod boot fails closed if it is set alongside
  `ENV=production` (see `specs/08`).
- CI scans every PR diff with gitleaks; run a full-history scan before publishing forks.
