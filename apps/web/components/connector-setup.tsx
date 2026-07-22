"use client";

import { useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import { actionError, readApiJson } from "@/lib/client-api";
import { isRecord } from "@/lib/guards";

import { RunwayLogo } from "./runway-logo";

type ConnectionState = "ready" | "checking" | "connected" | "attention";

type ConnectionResponse = {
  valid: boolean;
  detail: string;
};

function isConnectionResponse(value: unknown): value is ConnectionResponse {
  return (
    isRecord(value) &&
    typeof value.valid === "boolean" &&
    typeof value.detail === "string"
  );
}

export function ConnectorSetup({
  connectorEmail,
  configuredChannelId,
  browserName,
}: {
  connectorEmail: string;
  configuredChannelId: string;
  browserName: string;
}) {
  const defaultChannel = configuredChannelId
    ? `https://www.youtube.com/channel/${configuredChannelId}`
    : "";
  const [channelUrl, setChannelUrl] = useState(defaultChannel);
  const [copyLabel, setCopyLabel] = useState("Copy email");
  const [state, setState] = useState<ConnectionState>("ready");
  const [detail, setDetail] = useState(
    configuredChannelId
      ? "Saved channel loaded. Run the read-only check after the invitation has been accepted."
      : "Paste your channel URL after the invitation has been accepted.",
  );
  const requestLock = useRef(false);

  async function copyEmail() {
    try {
      await navigator.clipboard.writeText(connectorEmail);
      setCopyLabel("Email copied");
    } catch {
      setCopyLabel("Select the email");
    }
  }

  async function verifyConnection() {
    if (!channelUrl.trim() || requestLock.current) return;
    requestLock.current = true;
    setState("checking");
    setDetail(
      `Opening a read-only ${browserName} check for this channel. No post will be created.`,
    );
    try {
      const response = await fetch(
        `${API_URL}/api/publisher/connectors/validate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel_url: channelUrl.trim() }),
        },
      );
      const payload = await readApiJson(response, {
        validate: isConnectionResponse,
        failureMessage: "Runway could not verify Community posting capability.",
      });
      setState(payload.valid ? "connected" : "attention");
      setDetail(payload.detail);
    } catch (error) {
      setState("attention");
      setDetail(
        actionError(
          error,
          "Runway could not verify Community posting capability. Confirm the invitation was accepted and try again.",
        ),
      );
    } finally {
      requestLock.current = false;
    }
  }

  const stateLabel =
    state === "connected"
      ? "Connected"
      : state === "attention"
        ? "Needs attention"
        : state === "checking"
          ? "Checking access"
          : "Ready to verify";

  return (
    <div className="connector-page">
      <header className="connector-hero">
        <div className="connector-hero-copy">
          <p className="eyebrow">Connector / YouTube</p>
          <h1>Invite Runway backstage.</h1>
          <p className="lede">
            The channel owner chooses a declared role without sharing a password.
            Runway can observe posting controls in a read-only browser check, but it
            cannot read or prove the exact delegated role through an API.
          </p>
        </div>
        <section className="connector-pass" aria-label="Runway invitation account">
          <div>
            <RunwayLogo className="connector-pass-mark" />
            <span>
              <small>Runway connector account</small>
              <strong>{connectorEmail}</strong>
            </span>
          </div>
          <button type="button" onClick={copyEmail} aria-live="polite">
            {copyLabel}
          </button>
        </section>
      </header>

      <section className="connector-layout">
        <div className="connector-guide">
          <div className="connector-section-heading">
            <div>
              <p className="eyebrow">Set up access</p>
              <h2>Four steps. No passwords.</h2>
            </div>
            <a
              className="button secondary"
              href="https://studio.youtube.com"
              target="_blank"
              rel="noreferrer"
            >
              Open YouTube Studio ↗
            </a>
          </div>
          <ol className="connector-steps" aria-label="Channel connection steps">
            <li>
              <span>01</span>
              <div>
                <strong>Open channel permissions</strong>
                <p>
                  In YouTube Studio, open <b>Settings</b>, choose{" "}
                  <b>Permissions</b>, then select <b>Invite</b>.
                </p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <strong>Invite the Runway account</strong>
                <p>
                  Paste <code>{connectorEmail}</code> as the invited email address.
                </p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <strong>Choose Editor (limited)</strong>
                <p>
                  This role can create and publish posts without exposing channel
                  revenue. Send the invitation when the role is correct.
                </p>
              </div>
            </li>
            <li>
              <span>04</span>
              <div>
                <strong>Return and verify</strong>
                <p>
                  Once Runway has accepted the invite, paste the channel URL in the
                  capability check. YouTube invitations expire after 30 days.
                </p>
              </div>
            </li>
          </ol>
          <aside className="connector-boundary">
            <strong>You stay in control.</strong>
            <p>
            Editor access cannot manage channel permissions or delete published
            content. You can remove Runway at any time from the same Permissions
            screen. Runway treats confirmed external posts as immutable.
            </p>
          </aside>
        </div>

        <section
          className={`connector-check connector-state-${state}`}
          aria-labelledby="connector-check-title"
        >
          <div className="connector-check-topline">
            <span className="connector-status">
              <i aria-hidden="true" />
              {stateLabel}
            </span>
            <span>Read-only check</span>
          </div>
          <p className="eyebrow">Observed capability</p>
          <h2 id="connector-check-title">Are Community post controls available?</h2>
          <p>
            Runway checks that the saved account can open this channel and see its
            post creation controls. The check never publishes, edits, or deletes.
          </p>
          <label htmlFor="connector-channel">
            <span>YouTube channel URL, handle, or channel ID</span>
            <input
              id="connector-channel"
              name="channel-url"
              type="url"
              inputMode="url"
              value={channelUrl}
              onChange={(event) => setChannelUrl(event.target.value)}
              placeholder="youtube.com/@yourchannel"
              autoComplete="url"
              spellCheck={false}
              disabled={state === "checking"}
            />
          </label>
          <button
            className="button"
            type="button"
            onClick={verifyConnection}
            disabled={!channelUrl.trim() || state === "checking"}
            aria-busy={state === "checking"}
          >
            {state === "checking"
              ? "Checking posting capability…"
              : "Verify capability"}
          </button>
          <p
            className="connector-result"
            role={state === "attention" ? "alert" : "status"}
            aria-live={state === "attention" ? "assertive" : "polite"}
          >
            {detail}
          </p>
          <small>
            If Google requests sign-in or verification, complete it manually in the
            saved Runway browser profile, then repeat this check.
          </small>
        </section>
      </section>
    </div>
  );
}
