import { describe, expect, it, vi } from "vitest";

import {
  RunwayEventStream,
  streamUrl,
  type EventSourceLike,
  type RunwayEventStreamOptions,
} from "./event-stream";

class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];

  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  url: string;
  closed = false;
  listeners = new Map<string, Set<(event: MessageEvent) => void>>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.get(type)?.delete(listener);
  }

  close() {
    this.closed = true;
    this.readyState = 2;
  }

  open() {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }

  fail() {
    this.onerror?.(new Event("error"));
  }

  emit(type: string, data: object, lastEventId = "") {
    const event = {
      type,
      data: JSON.stringify(data),
      lastEventId,
    } as MessageEvent;
    this.listeners.get(type)?.forEach((listener) => listener(event));
  }
}

function makeStream(overrides: RunwayEventStreamOptions = {}) {
  FakeEventSource.instances = [];
  const options: RunwayEventStreamOptions = {
    eventSourceFactory: (url: string) => new FakeEventSource(url),
    ...overrides,
  };
  return { stream: new RunwayEventStream(options), options };
}

function last(): FakeEventSource {
  const instance = FakeEventSource.instances.at(-1);
  if (!instance) throw new Error("no EventSource instance");
  return instance;
}

describe("streamUrl", () => {
  it("carries last_event_id as a query parameter for fresh connections", () => {
    expect(streamUrl(41)).toContain("last_event_id=41");
    expect(streamUrl(null)).not.toContain("last_event_id");
  });
});

describe("RunwayEventStream", () => {
  it("delivers typed events and tracks the last event id for resume", () => {
    const onEvent = vi.fn();
    const { stream } = makeStream({ onEvent });
    stream.start();
    last().open();
    last().emit("target.state_changed", { subject_id: "9" }, "42");
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ id: 42, type: "target.state_changed" }),
    );
    // A fresh connection resumes from the last delivered id.
    stream.stop();
    stream.start();
    expect(last().url).toContain("last_event_id=42");
  });

  it("calls onResync for resync events", () => {
    const onResync = vi.fn();
    const { stream } = makeStream({ onResync });
    stream.start();
    last().open();
    last().emit("resync", { reason: "last_event_id_unavailable" }, "7");
    expect(onResync).toHaveBeenCalledTimes(1);
    stream.stop();
  });

  it("polls every pollIntervalMs after the failure threshold and recovers to live", () => {
    const ticks: Array<() => void> = [];
    const onPoll = vi.fn();
    const modes: string[] = [];
    const { stream } = makeStream({
      onPoll,
      onModeChange: (mode: string) => modes.push(mode),
      failureThreshold: 3,
      pollIntervalMs: 60_000,
      setInterval: (handler: () => void) => (ticks.push(handler), ticks.length),
      clearInterval: () => undefined,
    });
    stream.start();
    last().fail();
    last().fail();
    expect(modes).not.toContain("polling");
    last().fail();
    expect(last().closed).toBe(true);
    expect(modes).toContain("polling");

    // Each poll tick: onPoll runs and one fresh connection is attempted.
    expect(ticks).toHaveLength(1);
    ticks[0]();
    expect(onPoll).toHaveBeenCalledTimes(1);
    const retry = last();
    expect(retry.closed).toBe(false);
    retry.open();
    expect(modes.at(-1)).toBe("live");
    stream.stop();
    expect(modes.at(-1)).toBe("closed");
  });

  it("polls instead of crashing when no EventSource can be constructed", () => {
    const ticks: Array<() => void> = [];
    const onPoll = vi.fn();
    const { stream } = makeStream({
      onPoll,
      pollIntervalMs: 60_000,
      setInterval: (handler: () => void) => (ticks.push(handler), ticks.length),
      clearInterval: () => undefined,
      eventSourceFactory: () => {
        throw new ReferenceError("EventSource is not defined");
      },
    });
    stream.start();
    expect(stream.mode).toBe("polling");
    expect(ticks).toHaveLength(1);
    ticks[0]();
    expect(onPoll).toHaveBeenCalledTimes(1);
    stream.stop();
  });
});
