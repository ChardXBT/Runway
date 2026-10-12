// The web server binds to 127.0.0.1, but a page in the operator's browser can still
// reach it through a hostname that resolves to loopback (DNS rebinding). Only loopback
// Host headers are served so such a page cannot read Runway's rendered data.
const LOOPBACK_HOSTNAMES = new Set(["127.0.0.1", "localhost", "[::1]"]);

export function hostnameOf(hostHeader: string | null) {
  const host = (hostHeader ?? "").trim().toLowerCase();
  if (host.startsWith("[")) return host.slice(0, host.indexOf("]") + 1);
  const parts = host.split(":");
  return parts.length === 2 ? parts[0] : host;
}

export function isLoopbackHost(hostHeader: string | null) {
  return LOOPBACK_HOSTNAMES.has(hostnameOf(hostHeader));
}
