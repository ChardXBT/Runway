"use client";

import { useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import { actionError, readApiJson } from "@/lib/client-api";
import {
  isPublisherConnectionStatus,
  isPublisherQueueStatus,
} from "@/lib/guards";
import type {
  PublisherConnectionStatus,
  PublisherQueueStatus,
} from "@/lib/types";

function connectionTime(value: string | null) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unverified time" : date.toLocaleString();
}

function friendlyCheckLabel(value: string) {
  return value
    .replace("configured channel URL", "Configured Qlob channel")
    .replace("active Qlob identity", "Qlob identity selected")
    .replace("Qlob heading", "Qlob channel heading")
    .replace("Qlob posting access", "Community posting access");
}

export function PlatformConnection({
  publishingEnabled,
  channelId,
  connectorEmail,
  browserChannel,
  initialQueue,
  initialConnection,
}: {
  publishingEnabled: boolean;
  channelId: string;
  connectorEmail: string;
  browserChannel: string;
  initialQueue: PublisherQueueStatus;
  initialConnection: PublisherConnectionStatus;
}) {
  const [connection, setConnection] = useState(initialConnection);
  const [queue, setQueue] = useState(initialQueue);
  const [busy, setBusy] = useState<"check" | "resume" | null>(null);
  const actionLock = useRef(false);
  const [detailIsError, setDetailIsError] = useState(
    initialConnection.state === "needs_attention",
  );
  const [detail, setDetail] = useState(initialConnection.detail);

  async function checkConnection() {
    if (!publishingEnabled || actionLock.current) return;
    actionLock.current = true;
    setBusy("check");
    setDetailIsError(false);
    setDetail("Opening the private publisher profile and checking Qlob access…");
    try {
      const response = await fetch(`${API_URL}/api/publisher/session/validate`, {
        method: "POST",
      });
      const payload = await readApiJson(response, {
        validate: isPublisherConnectionStatus,
        failureMessage: "The YouTube connection check failed.",
      });
      setConnection(payload);
      setDetailIsError(payload.state === "needs_attention");
      setDetail(payload.detail);
    } catch (error) {
      const failure = actionError(
        error,
        "The YouTube connection check failed. The saved session was not marked connected.",
      );
      setConnection((current) => ({
        ...current,
        state: "needs_attention",
        valid: false,
        stale: false,
        detail: failure,
      }));
      setDetailIsError(true);
      setDetail(failure);
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  async function resumeQueue() {
    if (!publishingEnabled || actionLock.current) return;
    actionLock.current = true;
    setBusy("resume");
    setDetailIsError(false);
    setDetail("Requesting a safe queue recovery…");
    try {
      const response = await fetch(`${API_URL}/api/publisher/queue/resume`, {
        method: "POST",
      });
      const payload = await readApiJson(response, {
        validate: isPublisherQueueStatus,
        failureMessage: "YouTube scheduling could not resume.",
      });
      setQueue(payload);
      setDetailIsError(payload.paused);
      setDetail(
        payload.paused
          ? `The queue is still paused${payload.paused_reason ? `: ${payload.paused_reason}` : "."}`
          : payload.running
            ? "The publisher queue is running again."
            : payload.queued
              ? `${payload.queued} YouTube ${payload.queued === 1 ? "action is" : "actions are"} ready to process.`
              : "The publisher queue is ready; no actions are waiting.",
      );
    } catch (error) {
      setDetailIsError(true);
      setDetail(
        actionError(
          error,
          "YouTube scheduling could not resume. The queue state was not changed here.",
        ),
      );
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  const stateLabel = !publishingEnabled
    ? "Disabled"
    : connection.state === "connected"
      ? "Connected"
      : connection.state === "stale"
        ? "Check is stale"
        : connection.state === "needs_attention"
        ? "Needs attention"
        : "Ready to check";
  const stateClass =
    connection.state === "connected"
      ? "valid"
      : connection.state === "needs_attention"
        ? "invalid"
        : connection.state;
  const connectionChecks = Object.entries(connection.checks);

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
        <span className={`connection-state ${stateClass}`}>
          <i aria-hidden="true" />
          {stateLabel}
        </span>
      </div>
      <ol className="connection-steps" aria-label="YouTube connection setup">
        <li>
          <span>1</span>
          <div>
            <strong>Confirm Editor access</strong>
            <p>
              Invite <code>{connectorEmail}</code> to Qlob as Editor (Limited).
              This can create posts without exposing revenue data.
            </p>
          </div>
        </li>
        <li>
          <span>2</span>
          <div>
            <strong>Save the publisher login</strong>
            <p>
              Run <code>.\.venv\Scripts\runway.exe publisher login</code> in the
              Runway terminal, sign in inside the dedicated Chrome window, confirm
              Qlob’s Posts page, then close the window and press Enter.
            </p>
          </div>
        </li>
        <li>
          <span>3</span>
          <div>
            <strong>Check the saved session</strong>
            <p>
              The button below performs a real read-only session and channel access
              check. It does not create or schedule a post.
            </p>
          </div>
        </li>
        <li>
          <span>4</span>
          <div>
            <strong>Recover safely</strong>
            <p>
              If Google asks for sign-in or verification, Runway pauses. Repeat the
              publisher login step, check the session, then resume failed actions.
            </p>
          </div>
        </li>
      </ol>
      {connectionChecks.length > 0 && (
        <ul className="connection-checks" aria-label="Last verified capabilities">
          {connectionChecks.map(([label, passed]) => (
            <li className={passed ? "passed" : "failed"} key={label}>
              <span aria-hidden="true">{passed ? "✓" : "!"}</span>
              <span>{friendlyCheckLabel(label)}</span>
            </li>
          ))}
        </ul>
      )}
      <dl className="connection-facts">
        <div>
          <dt>Channel</dt>
          <dd>{channelId}</dd>
        </div>
        <div>
          <dt>Last checked</dt>
          <dd>{connectionTime(connection.checked_at)}</dd>
        </div>
        <div>
          <dt>Last verified post</dt>
          <dd>{connectionTime(connection.last_verified_publish_at)}</dd>
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
      <p
        className="connection-detail"
        role={detailIsError ? "alert" : "status"}
        aria-live={detailIsError ? "assertive" : "polite"}
      >
        {detail}
      </p>
      <div className="connection-actions">
        <button
          type="button"
          className="button"
          onClick={checkConnection}
          disabled={!publishingEnabled || busy !== null}
          aria-busy={busy === "check"}
        >
          {busy === "check" ? "Checking saved session…" : "Check saved session"}
        </button>
        {(queue.paused || connection.state === "needs_attention") && (
          <button
            type="button"
            className="button secondary"
            onClick={resumeQueue}
            disabled={!publishingEnabled || busy !== null}
            aria-busy={busy === "resume"}
          >
            {busy === "resume" ? "Resuming…" : "Resume failed actions"}
          </button>
        )}
      </div>
      <small>
        Publisher browser: {browserChannel === "chrome" ? "Google Chrome" : browserChannel}.
        Sign-in stays in Runway’s private profile; passwords and verification codes are
        never handled by the application. A connected check becomes stale after{" "}
        {connection.stale_after_hours} hours.
      </small>
    </section>
  );
}
