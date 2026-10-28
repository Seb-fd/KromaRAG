import { afterEach, describe, expect, it, vi } from "vitest";
import { POST } from "@/app/api/dev-token/route";

function req(body: unknown) {
  return new Request("http://localhost/api/dev-token", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("dev-token route guard", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("404s in production so nobody can self-mint roles", async () => {
    vi.stubEnv("NODE_ENV", "production");
    const res = await POST(req({ email: "a@b.com", role: "admin" }));
    expect(res.status).toBe(404);
  });

  it("rejects invalid email/role outside production", async () => {
    vi.stubEnv("NODE_ENV", "development");
    const res = await POST(req({ email: "not-an-email", role: "admin" }));
    expect(res.status).toBe(400);
  });

  it("mints a token for valid input outside production", async () => {
    vi.stubEnv("NODE_ENV", "development");
    const res = await POST(req({ email: "dev@example.com", role: "viewer" }));
    expect(res.status).toBe(200);
    const json = (await res.json()) as { token?: string };
    expect(typeof json.token).toBe("string");
    expect(json.token!.split(".")).toHaveLength(3);
  });
});
