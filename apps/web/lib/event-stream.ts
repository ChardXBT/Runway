"use client";

/** `/api/v1/events/stream` client with resume and a polling fallback.
 *
 * Frames carry `id:` (event row id), `event:` (the type) and `data:` (a JSON
 * object with `type`, `subject_type`, `subject_id`, `payload`, `created_at`).
 * The stream resumes with `Last-Event-ID`; EventSource sends that header
 * itself on its own reconnects, and the stream accepts `?last_event_id=` for
 * a fresh connection.
 *
 * Failure policy (spec 06 section 8): the browser's EventSource reconnects
 * transient drops itself. After `failureThreshold` consecutive error events
 * without an `open`, the stream stops reconnecting and calls `onPoll` every
 * `pollIntervalMs` (60s). Each poll tick also tries one fresh connection; a
 * successful `open` switches back to live mode.
 */

import { useEffect, useRef, useState } from "react";

import { API_URL } from "@/lib/api";

export interface RunwayStreamEvent {
  /** Event row id (`id:` frame), null for `resync` and untyped frames. */
  id: number | null;
  /** The `event:` value (`target.state_changed`, `resync`, …). */
  type: string;
  /** Parsed `data:` JSON (`subject_type`, `subject_id`, `payload`, …). */
  data: Record<string, unknown>;
}

export type StreamMode = "connecting" | "live" | "polling" | "closed";

export interface EventSourceLike {
  readyState: number;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  addEventListener(type: string, listener: (event: MessageEvent) => void): void;
  removeEventListener(type: string, listener: (event: MessageEvent) => void): void;
  close(): void;
}

export const EVENT_TYPES = [
  "account.updated",
  "sync.started",
  "sync.finished",
  "content.imported",
  "comment.new",
  "target.state_changed",
  "target.progress",
  "target.awaiting_release",
  "target.rejected",
  "media.ready",
  "quota.low",
  "resync",
] as const;

export interface RunwayEventStreamOptions {
  onEvent?: (event: RunwayStreamEvent) => void;
  /** A `resync` event means refetch what the view shows. */
  onResync?: () => void;
  onModeChange?: (mode: StreamMode) => void;
  /** Fallback work: called every `pollIntervalMs` while disconnected. */
  onPoll?: () => void;
  url?: string;
  eventSourceFactory?: (url: string) => EventSourceLike;
  failureThreshold?: number;
  pollIntervalMs?: number;
  /** Timer injection for tests. */
  setInterval?: (handler: () => void, ms: number) => unknown;
  clearInterval?: (id: unknown) => void;
}

export function streamUrl(lastEventId?: number | null): string {
  const base = `${API_URL}/api/v1/events/stream`;
  return lastEventId == null
    ? base
    : `${base}?last_event_id=${encodeURIComponent(lastEventId)}`;
}

function defaultFactory(url: string): EventSourceLike {
  return new EventSource(url) as EventSourceLike;
}

export class RunwayEventStream {
  private source: EventSourceLike | null = null;
  private lastEventId: number | null = null;
  private failures = 0;
  private pollTimer: unknown = null;
  private stopped = true;
  private readonly options: Required<
    Pick<
      RunwayEventStreamOptions,
      "failureThreshold" | "pollIntervalMs" | "setInterval" | "clearInterval"
    >
  > &
    RunwayEventStreamOptions;

  constructor(options: RunwayEventStreamOptions = {}) {
    this.options = {
      failureThreshold: 3,
      pollIntervalMs: 60_000,
      setInterval: (handler, ms) => setInterval(handler, ms),
      clearInterval: (id) => clearInterval(id as Parameters<typeof clearInterval>[0]),
      ...options,
    };
  }

  get mode(): StreamMode {
    if (this.stopped) return "closed";
    if (this.pollTimer !== null) return "polling";
    return this.source !== null && this.source.readyState === 1 ? "live" : "connecting";
  }

  private setMode(_mode: StreamMode) {
    this.options.onModeChange?.(this.mode);
  }

  private handleEvent = (raw: MessageEvent) => {
    const parsedId = Number.parseInt(String(raw.lastEventId ?? ""), 10);
    const id = Number.isFinite(parsedId) ? parsedId : null;
    if (id !== null) this.lastEventId = id;
    let data: Record<string, unknown> = {};
    try {
      const decoded: unknown = JSON.parse(String(raw.data ?? "{}"));
      if (typeof decoded === "object" && decoded !== null && !Array.isArray(decoded)) {
        data = decoded as Record<string, unknown>;
      }
    } catch {
      // A heartbeat or malformed frame carries no usable payload.
    }
    const type = raw.type || "message";
    if (type === "resync") {
      this.options.onResync?.();
    }
    this.options.onEvent?.({ id, type, data });
  };

  private open() {
    const factory = this.options.eventSourceFactory ?? defaultFactory;
    let source: EventSourceLike;
    try {
      source = factory(streamUrl(this.lastEventId));
    } catch {
      // No EventSource at all (or a factory that refuses): a stream cannot
      // exist in this environment, so the 60s poll becomes the transport.
      this.startPolling();
      return;
    }
    this.source = source;
    source.onmessage = this.handleEvent;
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, this.handleEvent);
    }
    source.onopen = () => {
      if (this.source !== source) return;
      this.failures = 0;
      this.stopPolling();
      this.setMode("live");
    };
    source.onerror = () => {
      if (this.source !== source) return;
      this.failures += 1;
      if (this.failures >= this.options.failureThreshold) {
        source.close();
        this.source = null;
        this.startPolling();
      }
    };
  }

  private startPolling() {
    if (this.pollTimer !== null || this.stopped) return;
    this.pollTimer = this.options.setInterval(() => {
      this.options.onPoll?.();
      // One fresh connection attempt per tick: a successful open returns to live.
      this.failures = 0;
      if (this.source === null && !this.stopped) this.open();
    }, this.options.pollIntervalMs);
    this.setMode("polling");
  }

  private stopPolling() {
    if (this.pollTimer === null) return;
    this.options.clearInterval(this.pollTimer);
    this.pollTimer = null;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.failures = 0;
    this.setMode("connecting");
    this.open();
  }

  stop() {
    this.stopped = true;
    this.stopPolling();
    if (this.source !== null) {
      const source = this.source;
      this.source = null;
      for (const type of EVENT_TYPES) {
        source.removeEventListener(type, this.handleEvent);
      }
      source.close();
    }
    this.setMode("closed");
  }
}

export interface UseEventStreamOptions extends RunwayEventStreamOptions {
  /** When false the stream stays closed. */
  enabled?: boolean;
}

/** React binding for `RunwayEventStream`; handlers may be inline functions. */
export function useEventStream(options: UseEventStreamOptions = {}): {
  mode: StreamMode;
} {
  const { enabled = true } = options;
  const [mode, setMode] = useState<StreamMode>("closed");
  const handlers = useRef(options);
  useEffect(() => {
    handlers.current = options;
  });
  const streamRef = useRef<RunwayEventStream | null>(null);

  useEffect(() => {
    if (!enabled) return;
    // Forward caller extras (url, factory, timers) only when present so the
    // constructor defaults stay intact; handlers go through the ref so they
    // may be inline functions.
    const { enabled: _enabled, ...extras } = handlers.current;
    void _enabled;
    const stream = new RunwayEventStream({
      ...extras,
      onEvent: (event) => handlers.current.onEvent?.(event),
      onResync: () => handlers.current.onResync?.(),
      onPoll: () => handlers.current.onPoll?.(),
      onModeChange: (next) => {
        handlers.current.onModeChange?.(next);
        setMode(next);
      },
    });
    streamRef.current = stream;
    stream.start();
    return () => {
      stream.stop();
      if (streamRef.current === stream) streamRef.current = null;
    };
    // The stream is created once; handler freshness comes from the ref above.
  }, [enabled]);

  return { mode };
}

/** Event types that mean "a post or one of its targets changed". */
export const POST_EVENT_TYPES = new Set([
  "target.state_changed",
  "target.progress",
  "target.awaiting_release",
  "target.rejected",
]);
