import { describe, expect, it } from "vitest";

import {
  isValidTimeZone,
  localDate,
  localTime,
  scheduleHasMinimumLead,
  scheduleInstant,
  scheduleIsPast,
  shiftIsoDate,
  zonedScheduleIso,
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
    expect(
      zonedScheduleIso("2026-07-15", "10:00", "America/Toronto"),
    ).toBe("2026-07-15T10:00:00-04:00");
    expect(localTime("2026-07-15T14:45:00Z", "America/Toronto")).toBe("10:45");
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

  it("treats naive API timestamps as UTC independent of the browser timezone", () => {
    const originalTimeZone = process.env.TZ;
    process.env.TZ = "America/Vancouver";
    try {
      expect(localDate("2026-11-01T05:30:00", "America/Toronto")).toBe(
        "2026-11-01",
      );
      expect(localTime("2026-11-01T05:30:00", "America/Toronto")).toBe("01:30");
      expect(localTime("2026-11-01", "America/Toronto")).toBe("20:00");
    } finally {
      if (originalTimeZone === undefined) delete process.env.TZ;
      else process.env.TZ = originalTimeZone;
    }
  });

  it("preserves explicit timezone offsets and fractional seconds", () => {
    expect(localTime("2026-11-01T05:30:00Z", "America/Toronto")).toBe("01:30");
    expect(localTime("2026-11-01T05:30:00+00:00", "America/Toronto")).toBe(
      "01:30",
    );
    expect(localTime("2026-11-01T01:30:00-04:00", "America/Toronto")).toBe(
      "01:30",
    );
    expect(localTime("2026-11-01T07:30:00+0200", "America/Toronto")).toBe(
      "01:30",
    );
    expect(localTime("2026-11-01T05:30:00.123Z", "America/Toronto")).toBe(
      "01:30",
    );
  });

  it("returns null for lost or invalid source timestamps instead of displaying epoch time", () => {
    expect(localDate(null, "America/Toronto")).toBeNull();
    expect(localTime(undefined, "America/Toronto")).toBeNull();
    expect(localDate("", "America/Toronto")).toBeNull();
    expect(localTime("   ", "America/Toronto")).toBeNull();
    expect(localDate(new Date("not-a-date"), "America/Toronto")).toBeNull();
  });

  it("enforces the same five-minute publishing lead-time boundary as the API", () => {
    const now = new Date("2026-07-18T14:00:00Z");
    expect(
      scheduleHasMinimumLead(
        "2026-07-18",
        "10:04",
        "America/Toronto",
        now,
      ),
    ).toBe(false);
    expect(
      scheduleHasMinimumLead(
        "2026-07-18",
        "10:05",
        "America/Toronto",
        now,
      ),
    ).toBe(false);
    expect(
      scheduleHasMinimumLead(
        "2026-07-18",
        "10:06",
        "America/Toronto",
        now,
      ),
    ).toBe(true);
  });

});
