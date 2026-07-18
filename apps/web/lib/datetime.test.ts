import { describe, expect, it } from "vitest";

import {
  isValidTimeZone,
  localDate,
  scheduleInstant,
  scheduleIsPast,
  shiftIsoDate,
} from "./datetime";

describe("configured-timezone scheduling", () => {
  it("validates against the configured timezone rather than the computer locale", () => {
    const now = new Date("2026-07-18T16:30:00Z");

    expect(
      scheduleIsPast(
        "2026-07-18",
        "10:00",
        "America/Toronto",
        now,
      ),
    ).toBe(true);
    expect(
      scheduleIsPast(
        "2026-07-18",
        "10:00",
        "America/Vancouver",
        now,
      ),
    ).toBe(false);
  });

  it("resolves daylight-saving offsets without a timezone dependency", () => {
    expect(
      scheduleInstant(
        "2026-01-15",
        "10:00",
        "America/Toronto",
      )?.toISOString(),
    ).toBe("2026-01-15T15:00:00.000Z");
    expect(
      scheduleInstant(
        "2026-07-15",
        "10:00",
        "America/Toronto",
      )?.toISOString(),
    ).toBe("2026-07-15T14:00:00.000Z");
  });

  it("rejects malformed dates, times, timezones, and nonexistent local times", () => {
    expect(isValidTimeZone("Not/A_Timezone")).toBe(false);
    expect(localDate("not-a-date", "America/Toronto")).toBeNull();
    expect(
      scheduleInstant("2026-03-08", "02:30", "America/Toronto"),
    ).toBeNull();
    expect(
      scheduleIsPast("2026-07-18", "25:00", "America/Toronto"),
    ).toBeNull();
  });

  it("shifts ISO dates across month boundaries", () => {
    expect(shiftIsoDate("2026-07-31", 1)).toBe("2026-08-01");
    expect(shiftIsoDate("2026-08-01", -1)).toBe("2026-07-31");
  });
});
