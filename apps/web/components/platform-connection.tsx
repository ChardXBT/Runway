"use client";

import { useState } from "react";

import { API_URL } from "@/lib/api";
import type { PublisherQueueStatus } from "@/lib/types";

type ConnectionState = "unchecked" | "valid" | "invalid";

export function PlatformConnection({
  publishingEnabled,
  channelId,
  browserChannel,
  initialQueue,
}: {
  publishingEnabled: boolean;
  channelId: string;
  browserChannel: string;
  initialQueue: PublisherQueueStatus;
}) {
  const [connection, setConnection] = useState<ConnectionState>("unchecked");
  const [queue, setQueue] = useState(initialQueue);
  const [busy, setBusy] = useState<"check" | "resume" | null>(null);
  const [detail, setDetail] = useState(
    publishingEnabled
      ? "Run a connection check before the first live scheduling session."
      : "Enable publishing in the local environment before checking YouTube.",
  );

  async function checkConnection() {
    if (!publishingEnabled || busy) return;
    setBusy("check");
    setDetail("Opening the private publisher profile and checking Qlob access…");
    try {
      const response = await fetch(`${API_URL}/api/publisher/session/validate`, {
        method: "POST",
      });
      const payload = (await response.json()) as {
        valid?: boolean;
        detail?: string;
      };
      if (!response.ok) {
        throw new Error(payload.detail || "The YouTube connection check failed.");
      }
      setConnection(payload.valid ? "valid" : "invalid");
      setDetail(payload.detail || "YouTube connection checked.");
    } catch (error) {
      setConnection("invalid");
      setDetail(
        error instanceof Error ? error.message : "The YouTube connection check failed.",
      );
    } finally {
      setBusy(null);
    }
  }

  async function resumeQueue() {
    if (!publishingEnabled || busy) return;
    setBusy("resume");
    try {
      const response = await fetch(`${API_URL}/api/publisher/queue/resume`, {
        method: "POST",
      });
      const payload = (await response.json()) as PublisherQueueStatus & {
        detail?: string;
      };
      if (!response.ok) {
        throw new Error(payload.detail || "YouTube scheduling could not resume.");
      }
      setQueue(payload);
      setDetail("The publisher queue is running again.");
    } catch (error) {
      setDetail(
        error instanceof Error ? error.message : "YouTube scheduling could not resume.",
      );
    } finally {
      setBusy(null);
    }
  }

  const stateLabel = !publishingEnabled
    ? "Disabled"
    : connection === "valid"
      ? "Connected"
      : connection === "invalid"
        ? "Needs attention"
        : "Ready to check";

  return (
    <section
      id="platform-connection"
      className="panel connection-panel"
      aria-labelledby="connection-title"
    >
      <div className="connection-heading">
        <div>
          <p className="eyebrow">Platform connection</p>
          <h2 id="connection-title">YouTube · Qlob</h2>
        </div>
        <span className={`connection-state ${connection}`}>
          <i aria-hidden="true" />
          {stateLabel}
        </span>
      </div>
      <dl className="connection-facts">
        <div>
          <dt>Channel</dt>
          <dd>{channelId}</dd>
        </div>
        <div>
          <dt>Browser</dt>
          <dd>{browserChannel === "chrome" ? "Google Chrome" : browserChannel}</dd>
        </div>
        <div>
          <dt>Automation</dt>
          <dd>One RunWay post daily at the configured Eastern time</dd>
        </div>
        <div>
          <dt>Queue</dt>
          <dd>
            {queue.running
              ? "Scheduling now"
              : queue.paused
                ? "Paused"
                : `${queue.queued} waiting`}
          </dd>
        </div>
      </dl>
      <p className="connection-detail" role="status">
        {detail}
      </p>
      <div className="connection-actions">
        <button
          type="button"
          className="button"
          onClick={checkConnection}
          disabled={!publishingEnabled || busy !== null}
        >
          {busy === "check" ? "Checking connection…" : "Check connection"}
        </button>
        {(queue.paused || connection === "invalid") && (
          <button
            type="button"
            className="button secondary"
            onClick={resumeQueue}
            disabled={!publishingEnabled || busy !== null}
          >
            {busy === "resume" ? "Resuming…" : "Resume failed actions"}
          </button>
        )}
      </div>
      <small>
        Sign-in stays in RunWay’s private Chrome profile. Passwords and verification codes
        are never handled by the application.
      </small>
    </section>
  );
}
