function dateParts(value: Date, timeZone: string) {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone,
  });
  const parts = formatter.formatToParts(value);
  const get = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((part) => part.type === type)?.value ?? "";
  return {
    year: get("year"),
    month: get("month"),
    day: get("day"),
  };
}

export function isValidTimeZone(value: string) {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value }).format();
    return true;
  } catch {
    return false;
  }
}

export function localDate(value: string | Date, timeZone: string) {
  try {
    const date = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(date.getTime()) || !isValidTimeZone(timeZone)) return null;
    const parts = dateParts(date, timeZone);
    return `${parts.year}-${parts.month}-${parts.day}`;
  } catch {
    return null;
  }
}

export function localTime(value: string | Date, timeZone: string) {
  try {
    const date = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(date.getTime()) || !isValidTimeZone(timeZone)) return null;
    return new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
      timeZone,
    }).format(date);
  } catch {
    return null;
  }
}

function zonedPartsAsUtc(value: Date, timeZone: string) {
  const formatter = new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    timeZone,
  });
  const parts = formatter.formatToParts(value);
  const number = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((part) => part.type === type)?.value);
  return Date.UTC(
    number("year"),
    number("month") - 1,
    number("day"),
    number("hour"),
    number("minute"),
    number("second"),
  );
}

export function scheduleInstant(
  dateValue: string,
  timeValue: string,
  timeZone: string,
) {
  const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateValue);
  const timeMatch = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(timeValue);
  if (!dateMatch || !timeMatch || !isValidTimeZone(timeZone)) return null;

  const desired = Date.UTC(
    Number(dateMatch[1]),
    Number(dateMatch[2]) - 1,
    Number(dateMatch[3]),
    Number(timeMatch[1]),
    Number(timeMatch[2]),
  );
  let instant = desired;
  try {
    for (let index = 0; index < 3; index += 1) {
      const difference = desired - zonedPartsAsUtc(new Date(instant), timeZone);
      if (difference === 0) break;
      instant += difference;
    }
  } catch {
    return null;
  }

  const resolved = new Date(instant);
  const resolvedDate = localDate(resolved, timeZone);
  const resolvedTime = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone,
  }).format(resolved);
  if (resolvedDate !== dateValue || resolvedTime !== timeValue) return null;
  return resolved;
}

export function scheduleIsPast(
  dateValue: string,
  timeValue: string,
  timeZone: string,
  now = new Date(),
) {
  const instant = scheduleInstant(dateValue, timeValue, timeZone);
  if (!instant) return null;
  return instant.getTime() <= now.getTime();
}

export function zonedScheduleIso(
  dateValue: string,
  timeValue: string,
  timeZone: string,
) {
  const instant = scheduleInstant(dateValue, timeValue, timeZone);
  if (!instant) return null;
  const offsetMinutes = Math.round(
    (zonedPartsAsUtc(instant, timeZone) - instant.getTime()) / 60_000,
  );
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const absolute = Math.abs(offsetMinutes);
  const offset = `${sign}${String(Math.floor(absolute / 60)).padStart(2, "0")}:${String(absolute % 60).padStart(2, "0")}`;
  return `${dateValue}T${timeValue}:00${offset}`;
}

export function shiftIsoDate(value: string, days: number) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return null;
  date.setUTCDate(date.getUTCDate() + days);
  return [
    date.getUTCFullYear(),
    String(date.getUTCMonth() + 1).padStart(2, "0"),
    String(date.getUTCDate()).padStart(2, "0"),
  ].join("-");
}
