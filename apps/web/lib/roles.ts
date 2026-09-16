/** Shared role vocabulary — importable from client AND server (no next-auth imports here). */
export const KROMA_ROLES = ["admin", "editor", "viewer"] as const;
export type KromaRole = (typeof KROMA_ROLES)[number];
export const isRole = (r: unknown): r is KromaRole =>
  typeof r === "string" && (KROMA_ROLES as readonly string[]).includes(r);
