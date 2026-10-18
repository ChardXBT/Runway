import { describe, expect, it } from "vitest";

import {
  formatAbsoluteTime,
  formatCompactNumber,
  formatDelta,
  formatDurationSeconds,
  formatHandle,
  formatInteger,
  formatPercent,
  formatRelativeTime,
  parseApiDate,
} from "./format";

describe("formatCompactNumber", () => {
  it("formats thousands, millions, and small values", () => {
    expect(formatCompactNumber(1200)).toBe("1.2K");
    expect(formatCompactNumber(3_400_000)).toBe("3.4M");
    expect(formatCompactNumber(999)).toBe("999");
    expect(formatCompactNumber(12_500)).toBe("12.5K");
    expect(formatCompactNumber(1_200_000_000)).toBe("1.2B");
    expect(formatCompactNumber(null)).toBe("—");
    expect(formatCompactNumber(-1500)).toBe("-1.5K");
  });
});

describe("parseApiDate", () => {
  it("treats naive ISO strings as UTC", () => {
    const parsed = parseApiDate("2026-07-20T11:56:00");
    expect(parsed?.toISOString()).toBe("2026-07-20T11:56:00.000Z");
  });

  it("respects explicit offsets and rejects garbage", () => {
    expect(parseApiDate("2026-07-20T11:56:00+02:00")?.getUTCHours()).toBe(9);
    expect(parseApiDate("2026-07-20T11:56:00Z")?.getUTCHours()).toBe(11);
    expect(parseApiDate("not-a-date")).toBeNull();
    expect(parseApiDate(null)).toBeNull();
  });

  it("feeds naive timestamps into formatRelativeTime as UTC", () => {
    const now = new Date("2026-07-20T12:00:00Z");
    expect(formatRelativeTime("2026-07-20T11:56:00", now)).toBe(
      "4 minutes ago",
    );
  });
});

describe("formatRelativeTime", () => {
  const now = new Date("2026-07-20T12:00:00Z");

  it("renders recent and older times", () => {
    expect(
      formatRelativeTime("2026-07-20T11:56:00Z", now),
    ).toBe("4 minutes ago");
    expect(formatRelativeTime("2026-07-20T11:59:59Z", now)).toBe("just now");
    expect(formatRelativeTime("2026-07-19T12:00:00Z", now)).toBe("1 day ago");
    expect(formatRelativeTime(null, now)).toBe("never");
    expect(formatRelativeTime("not-a-date", now)).toBe("never");
    expect(formatRelativeTime("2026-07-20T13:00:00Z", now)).toBe("in 1 hour");
  });
});

describe("misc helpers", () => {
  it("formats percentages and durations", () => {
    expect(formatPercent(0.1234)).toBe("12.3%");
    expect(formatPercent(null)).toBe("—");
    expect(formatDurationSeconds(754)).toBe("12:34");
    expect(formatDurationSeconds(3723)).toBe("1:02:03");
    expect(formatDurationSeconds(undefined)).toBe("—");
  });

  it("formats deltas with a sign", () => {
    expect(formatDelta(1234)).toBe("+1.2K");
    expect(formatDelta(-50)).toBe("−50");
    expect(formatDelta(0)).toBe("0");
    expect(formatDelta(null)).toBe("");
  });

  it("formats integers and absolute times", () => {
    expect(formatInteger(1234567)).toBe("1,234,567");
    expect(formatInteger(null)).toBe("—");
    expect(formatAbsoluteTime("2026-07-20T15:04:00Z")).not.toBe("");
    expect(formatAbsoluteTime(null)).toBe("");
  });

  it("normalizes handles with exactly one @", () => {
    expect(formatHandle("name")).toBe("@name");
    expect(formatHandle("@name")).toBe("@name");
    expect(formatHandle("@@name")).toBe("@name");
    expect(formatHandle(" @name ")).toBe("@name");
    expect(formatHandle("@")).toBe("");
    expect(formatHandle(null)).toBe("");
    expect(formatHandle("")).toBe("");
  });
});
