"use client";

import { useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import { actionError, readApiJson } from "@/lib/client-api";
import { isPublisherQueueStatus, isRecord } from "@/lib/guards";
import type { PublisherQueueStatus } from "@/lib/types";

type ConnectionState = "unchecked" | "valid" | "invalid";

type ConnectionResponse = {
  valid: boolean;
  detail?: string;
};

function isConnectionResponse(value: unknown): value is ConnectionResponse {
  return (
    isRecord(value) &&
    typeof value.valid === "boolean" &&
    (value.detail === undefined || typeof value.detail === "string")
  );
}

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
  const actionLock = useRef(false);
  const [detailIsError, setDetailIsError] = useState(false);
  const [detail, setDetail] = useState(
    publishingEnabled
      ? "Run a connection check before the first live scheduling session."
      : "Enable publishing in the local environment before checking YouTube.",
  );

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
        validate: isConnectionResponse,
        failureMessage: "The YouTube connection check failed.",
      });
      setConnection(payload.valid ? "valid" : "invalid");
      setDetailIsError(!payload.valid);
      setDetail(
        payload.detail ||
          (payload.valid
            ? "The saved Qlob Editor session is ready."
            : "The saved session does not currently have usable Qlob access."),
      );
    } catch (error) {
      setConnection("invalid");
      setDetailIsError(true);
      setDetail(
        actionError(
          error,
          "The YouTube connection check failed. The saved session was not marked connected.",
        ),
      );
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
      <ol className="connection-steps" aria-label="YouTube connection setup">
        <li>
          <span>1</span>
          <div>
            <strong>Confirm Editor access</strong>
            <p>
              Use the Google account YouTube identifies as an Editor for the Qlob
              channel.
            </p>
          </div>
        </li>
        <li>
          <span>2</span>
          <div>
            <strong>Save the publisher login</strong>
            <p>
              Run <code>.\.venv\Scripts\runway.exe publisher login</code> in the
              RunWay terminal, sign in inside the dedicated Chrome window, confirm
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
              If Google asks for sign-in or verification, RunWay pauses. Repeat the
              publisher login step, check the session, then resume failed actions.
            </p>
          </div>
        </li>
      </ol>
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
        {(queue.paused || connection === "invalid") && (
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
        Sign-in stays in RunWay’s private Chrome profile. Passwords and verification codes
        are never handled by the application.
      </small>
    </section>
  );
}
