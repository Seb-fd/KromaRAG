"use server";

import { auth, clerkClient } from "@clerk/nextjs/server";
import { isRole } from "@/lib/roles";

/**
 * First-time role assignment for self-serve sign-ups.
 *
 * Security guard (Option A, approved):
 * - Only `viewer` or `editor` can be self-assigned. `admin` is rejected to
 *   prevent privilege escalation — admins are promoted via the Clerk
 *   Dashboard (Users → Metadata → publicMetadata.roles) by an existing admin.
 * - Never overwrites: if `publicMetadata.roles` is already a non-empty array,
 *   the call is a no-op (`applied: false`). Existing users keep the role an
 *   admin assigned; signing in never changes roles.
 * - Authenticated + self-only: the caller can only set their own user id from
 *   `auth()` (Clerk secret stays server-side via `clerkClient()`).
 *
 * Call this AFTER the session is active (post `finalize()`), because `auth()`
 * requires a signed-in user. For the email-code flow that means: create →
 * verify code → finalize → setInitialRole. For Google OAuth it means: lazily
 * from AppShell once Clerk lands on `/` (see AppShell OAuth role sync) —
 * never inside /login/sso-callback, which must stay a bare
 * <AuthenticateWithRedirectCallback/> so session establishment is unblocked.
 */
export async function setInitialRole(
  role: string,
): Promise<{ ok: true; applied: boolean }> {
  if (!isRole(role) || role === "admin") {
    throw new Error(
      "Admin access can't be self-assigned. Choose viewer or editor — an existing admin can promote you later.",
    );
  }

  const { userId } = await auth();
  if (!userId) {
    throw new Error("Not signed in — complete verification first.");
  }

  const client = await clerkClient();
  const user = await client.users.getUser(userId);
  const meta = (user.publicMetadata ?? {}) as Record<string, unknown>;

  if (Array.isArray(meta.roles) && meta.roles.length > 0) {
    // Roles already assigned (e.g. by an admin or a previous sign-up).
    // Never downgrade/overwrite from a self-serve call.
    return { ok: true, applied: false };
  }

  await client.users.updateUserMetadata(userId, {
    publicMetadata: { ...meta, roles: [role] },
  });
  return { ok: true, applied: true };
}
