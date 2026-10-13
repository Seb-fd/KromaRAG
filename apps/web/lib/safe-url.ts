/** Safe external href allowlist (specs/02 + 08 SEC-06 frontend).
 * LLM / ingested-doc markdown can contain `javascript:` / `data:` links.
 * Only http/https/mailto/anchor/relative pass; everything else → undefined
 * and the caller must render plain text instead of <a>.
 */
const SAFE_HREF_RE = /^(https?:\/\/|mailto:|#|\/)/i;
const BLOCKED_RE = /^(javascript|data|vbscript|file|ftp|gopher):/i;

export function isSafeHref(href: string | undefined | null): href is string {
  if (!href) return false;
  const h = href.trim();
  if (!h) return false;
  if (BLOCKED_RE.test(h)) return false;
  return SAFE_HREF_RE.test(h);
}

export function safeHref(href: string | undefined | null): string | undefined {
  if (!href) return undefined;
  const h = href.trim();
  return isSafeHref(h) ? h : undefined;
}
