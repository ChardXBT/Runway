import { describe, expect, it } from "vitest";

import { ApiError, readApiJson } from "./client-api";

const isResult = (value: unknown): value is { saved: boolean } =>
  typeof value === "object" &&
  value !== null &&
  "saved" in value &&
  value.saved === true;

describe("readApiJson", () => {
  it("returns only a validated successful payload", async () => {
    const result = await readApiJson(
      {
        ok: true,
        json: async () => ({ saved: true }),
      } as Response,
      {
        validate: isResult,
        failureMessage: "Save failed.",
      },
    );

    expect(result).toEqual({ saved: true });
  });

  it("uses a clean API detail for a failed response", async () => {
    await expect(
      readApiJson(
        {
          ok: false,
          json: async () => ({ detail: "The date is occupied." }),
        } as Response,
        {
          validate: isResult,
          failureMessage: "Save failed.",
        },
      ),
    ).rejects.toMatchObject({
      message: "The date is occupied.",
      uncertainOutcome: false,
    });
  });

  it("marks malformed success responses as uncertain", async () => {
    let error: unknown;
    try {
      await readApiJson(
        {
          ok: true,
          json: async () => ({ saved: "maybe" }),
        } as Response,
        {
          validate: isResult,
          failureMessage: "Save failed.",
        },
      );
    } catch (caught) {
      error = caught;
    }

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ uncertainOutcome: true });
  });

  it("handles non-JSON failures without claiming success", async () => {
    await expect(
      readApiJson(
        {
          ok: false,
          json: async () => {
            throw new SyntaxError("HTML response");
          },
        } as unknown as Response,
        {
          validate: isResult,
          failureMessage: "Save failed.",
        },
      ),
    ).rejects.toMatchObject({
      message: "Save failed.",
      uncertainOutcome: false,
    });
  });
});
