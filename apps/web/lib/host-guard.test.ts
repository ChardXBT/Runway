import { describe, expect, it } from "vitest";

import { hostnameOf, isLoopbackHost } from "./host-guard";

describe("isLoopbackHost", () => {
  it.each(["127.0.0.1:3000", "localhost:3000", "LOCALHOST", "[::1]:3000", "127.0.0.1"])(
    "serves %s",
    (host) => expect(isLoopbackHost(host)).toBe(true),
  );

  it.each([
    "attacker.example:3000",
    "127.0.0.1.attacker.example",
    "localhost.attacker.example:3000",
    "0.0.0.0:3000",
    "",
    null,
  ])("refuses %s", (host) => expect(isLoopbackHost(host)).toBe(false));

  it("keeps bracketed IPv6 hosts intact", () => {
    expect(hostnameOf("[::1]:3000")).toBe("[::1]");
  });
});
