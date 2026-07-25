import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiReadError, apiGet, apiGetOptional, apiGetRequired } from "./api";

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

  it("throws instead of fabricating empty required data when the API is offline", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));

    await expect(
      apiGetRequired("/api/example", (value): value is unknown[] =>
        Array.isArray(value),
      ),
    ).rejects.toMatchObject({
      name: "ApiReadError",
      status: null,
    } satisfies Partial<ApiReadError>);
  });

  it("treats only a real 404 as an absent optional record", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ status: 404, ok: false })
      .mockResolvedValueOnce({ status: 503, ok: false });
    vi.stubGlobal("fetch", fetchMock);
    const validator = (value: unknown): value is { id: number } =>
      typeof value === "object" && value !== null && "id" in value;

    await expect(apiGetOptional("/api/example/1", validator)).resolves.toBeNull();
    await expect(apiGetOptional("/api/example/2", validator)).rejects.toMatchObject({
      status: 503,
    });
  });

  it("rejects malformed required payloads instead of passing them to a page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        status: 200,
        ok: true,
        json: async () => ({ ready: "yes" }),
      }),
    );

    await expect(
      apiGetRequired(
        "/api/example",
        (value): value is { ready: boolean } =>
          typeof value === "object" &&
          value !== null &&
          "ready" in value &&
          typeof value.ready === "boolean",
      ),
    ).rejects.toThrow("unexpected response");
  });
});
