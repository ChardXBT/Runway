import { afterEach, describe, expect, it, vi } from "vitest";

import { apiGet } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiGet", () => {
  it("falls back on network and malformed JSON failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    await expect(apiGet("/api/example", [])).resolves.toEqual([]);

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => {
          throw new SyntaxError("bad JSON");
        },
      }),
    );
    await expect(apiGet("/api/example", { ready: false })).resolves.toEqual({
      ready: false,
    });
  });

  it("does not pass a malformed success payload into a page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ ready: "yes" }),
      }),
    );

    const result = await apiGet(
      "/api/example",
      { ready: false },
      (value): value is { ready: boolean } =>
        typeof value === "object" &&
        value !== null &&
        "ready" in value &&
        typeof value.ready === "boolean",
    );

    expect(result).toEqual({ ready: false });
  });
});
