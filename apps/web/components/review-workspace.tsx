"use client";
/* eslint-disable @next/next/no-img-element */

import { useMemo, useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import {
  actionError,
  hasUncertainOutcome,
  readApiJson,
} from "@/lib/client-api";
import {
  isEditorialEnvelope,
  isProposal,
  isPublisherQueueStatus,
} from "@/lib/guards";
import type {
  EditorialEnvelope,
  Proposal,
  PublisherQueueStatus,
  WorkflowStatus,
} from "@/lib/types";

type Action =
  | "accept"
  | "reject"
  | "options"
  | "resume"
  | "regenerate"
  | "replace";

function emptyQueue(): PublisherQueueStatus {
  return {
    running: false,
    queued: 0,
    paused: false,
    paused_reason: null,
  };
}

export function ReviewWorkspace({
  initialProposal,
  initialWorkflow,
  initialPublisherQueue,
  publishingEnabled,
}: {
  initialProposal: Proposal | null;
  initialWorkflow: WorkflowStatus;
  initialPublisherQueue?: PublisherQueueStatus;
  publishingEnabled: boolean;
}) {
  const [proposal, setProposal] = useState(initialProposal);
  const [caption, setCaption] = useState(initialProposal?.final_caption ?? "");
  const [workflow, setWorkflow] = useState(initialWorkflow);
  const [publisherQueue, setPublisherQueue] = useState(
    initialPublisherQueue ?? emptyQueue(),
  );
  const [busy, setBusy] = useState<Action | null>(null);
  const [editing, setEditing] = useState(false);
  const [message, setMessage] = useState("");
  const [messageIsError, setMessageIsError] = useState(false);
  const [decisionUncertain, setDecisionUncertain] = useState(false);
  const [sessionDecisions, setSessionDecisions] = useState(0);
  const warmingTray = useRef(false);
  const actionLock = useRef(false);
  const captionRef = useRef<HTMLTextAreaElement>(null);
  const slotFormatter = useMemo(() => {
    try {
      return new Intl.DateTimeFormat("en-US", {
        weekday: "short",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
        timeZone: workflow.timezone,
      });
    } catch {
      return null;
    }
  }, [workflow.timezone]);

  function setNotice(text: string, error = false) {
    setMessage(text);
    setMessageIsError(error);
  }

  function showProposal(next: Proposal | null) {
    setProposal(next);
    setCaption(next?.final_caption ?? "");
    setEditing(false);
    setDecisionUncertain(false);
  }

  async function parseResponse(response: Response) {
    return readApiJson(response, {
      validate: isEditorialEnvelope,
      failureMessage: "RunWay could not complete that decision.",
    });
  }

  async function parseProposalResponse(response: Response) {
    return readApiJson(response, {
      validate: isProposal,
      failureMessage: "RunWay could not refresh this option.",
    });
  }

  async function requestOptions() {
    const response = await fetch(`${API_URL}/api/editorial/options/ensure`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: 5, live_discovery: true }),
    });
    const payload = await parseResponse(response);
    setWorkflow(payload.workflow);
    showProposal(payload.next_proposal);
    setNotice(
      payload.next_proposal
        ? "The next option is ready."
        : payload.detail || "No accepted options were found.",
    );
  }

  async function ensureOptions() {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy("options");
    setNotice("Preparing the next looks…");
    try {
      await requestOptions();
    } catch (error) {
      setNotice(
        actionError(error, "Could not find more options. Try again when the local service is ready."),
        true,
      );
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  async function warmTray() {
    if (warmingTray.current) return;
    warmingTray.current = true;
    try {
      const response = await fetch(`${API_URL}/api/editorial/options/ensure`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: 5, live_discovery: true }),
      });
      await parseResponse(response);
    } catch {
      // Empty-runway refill remains the visible recovery path.
    } finally {
      warmingTray.current = false;
    }
  }

  async function finishDecision(payload: EditorialEnvelope, notice: string) {
    setWorkflow(payload.workflow);
    if (payload.publisher_queue) setPublisherQueue(payload.publisher_queue);
    setSessionDecisions((value) => value + 1);
    setNotice(notice);
    if (payload.next_proposal) {
      showProposal(payload.next_proposal);
      if (payload.workflow.needs_review <= 2) void warmTray();
      return;
    }
    showProposal(null);
    setNotice(`${notice} Preparing the next option…`);
    try {
      await requestOptions();
    } catch (error) {
      setNotice(
        `${notice} ${actionError(
          error,
          "The next option could not be loaded. Use Load more options to continue.",
        )}`,
        true,
      );
    }
  }

  async function accept() {
    if (
      !proposal ||
      actionLock.current ||
      decisionUncertain ||
      !caption.trim()
    ) {
      return;
    }
    actionLock.current = true;
    setBusy("accept");
    setNotice("");
    try {
      const response = await fetch(
        `${API_URL}/api/editorial/proposals/${proposal.id}/approve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ final_caption: caption }),
        },
      );
      const payload = await parseResponse(response);
      const scheduledAt = payload.proposal?.scheduled_publish_at;
      await finishDecision(
        payload,
        scheduledAt
          ? `Accepted for ${
              slotFormatter?.format(new Date(scheduledAt)) ??
              "the configured Lineup slot"
            }.`
          : "Accepted and added to Lineup.",
      );
    } catch (error) {
      const uncertain = hasUncertainOutcome(error);
      setDecisionUncertain(uncertain);
      setNotice(
        actionError(
          error,
          "Accept failed. Your caption is still here.",
          "The accept response could not be verified. Your caption is preserved; reload Generator and check Lineup before taking another decision.",
        ),
        true,
      );
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  async function reject() {
    if (!proposal || actionLock.current || decisionUncertain) return;
    actionLock.current = true;
    setBusy("reject");
    setNotice("");
    try {
      const response = await fetch(
        `${API_URL}/api/editorial/proposals/${proposal.id}/reject`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "The complete option was not a fit." }),
        },
      );
      await finishDecision(
        await parseResponse(response),
        "Rejected. The negative signal is saved.",
      );
    } catch (error) {
      const uncertain = hasUncertainOutcome(error);
      setDecisionUncertain(uncertain);
      setNotice(
        actionError(
          error,
          "Rejection failed. This option is still on screen.",
          "The rejection response could not be verified. Reload Generator before making another decision.",
        ),
        true,
      );
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  async function regenerateCaptions() {
    if (!proposal || actionLock.current || decisionUncertain) return;
    actionLock.current = true;
    setBusy("regenerate");
    setNotice("Generating fresh captions from the same image…");
    try {
      const response = await fetch(
        `${API_URL}/api/proposals/${proposal.id}/regenerate`,
        { method: "POST" },
      );
      const refreshed = await parseProposalResponse(response);
      showProposal(refreshed);
      setNotice("Fresh captions are ready.");
    } catch (error) {
      setNotice(
        actionError(error, "Caption regeneration failed. The current caption is preserved."),
        true,
      );
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  async function replaceImage() {
    if (!proposal || actionLock.current || decisionUncertain) return;
    actionLock.current = true;
    setBusy("replace");
    setNotice("Finding another image and generating its captions…");
    try {
      const response = await fetch(
        `${API_URL}/api/proposals/${proposal.id}/replace`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ candidate_id: null }),
        },
      );
      const refreshed = await parseProposalResponse(response);
      showProposal(refreshed);
      setNotice("A replacement image and its captions are ready.");
    } catch (error) {
      setNotice(
        actionError(error, "Image replacement failed. The current option is preserved."),
        true,
      );
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  function toggleEdit() {
    const next = !editing;
    setEditing(next);
    if (next) {
      requestAnimationFrame(() => {
        captionRef.current?.focus();
        captionRef.current?.setSelectionRange(caption.length, caption.length);
      });
    }
  }

  async function resumePublisher() {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy("resume");
    setNotice("Requesting a safe queue recovery…");
    try {
      const response = await fetch(`${API_URL}/api/publisher/queue/resume`, {
        method: "POST",
      });
      const payload = await readApiJson(response, {
        validate: isPublisherQueueStatus,
        failureMessage: "The publisher queue could not resume.",
      });
      setPublisherQueue(payload);
      setNotice(
        payload.paused
          ? "The publisher queue is still paused. Check the saved YouTube session."
          : payload.running
            ? "YouTube scheduling resumed."
            : "The publisher queue is ready.",
        payload.paused,
      );
    } catch (error) {
      setNotice(
        actionError(error, "Could not resume scheduling. The queue remains unchanged here."),
        true,
      );
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  const nextSlot =
    slotFormatter?.format(new Date(workflow.next_available_at)) ??
    "Next configured opening";
  const topic = proposal?.candidate?.detected_topic;

  return (
    <main className="editorial-conveyor">
      <header className="conveyor-header">
        <div>
          <p className="eyebrow">Generator / Qlob</p>
          <h1>{proposal ? "Choose the next post." : "Generator clear."}</h1>
        </div>
        <div className="conveyor-stats" aria-label="Editorial session status">
          <span>
            <strong>{sessionDecisions}</strong> decisions
          </span>
          <span>
            <strong>{workflow.queued + workflow.scheduled}</strong> in Lineup
          </span>
          <span>
            <strong>{workflow.needs_review}</strong> ready
          </span>
        </div>
      </header>

      {publisherQueue.paused && (
        <section className="queue-pause" role="alert">
          <div>
            <strong>YouTube is waiting.</strong>
            <span>{publisherQueue.paused_reason}</span>
          </div>
          <button
            type="button"
            className="button secondary"
            disabled={busy !== null}
            onClick={resumePublisher}
            aria-busy={busy === "resume"}
          >
            {busy === "resume" ? "Resuming…" : "Resume after sign-in"}
          </button>
        </section>
      )}

      {proposal ? (
        <>
          <ol className="generator-loop" aria-label="Generator workflow">
            <li className="complete">Discover image</li>
            <li className="complete">Generate caption</li>
            <li className="active">Review</li>
            <li>Reject / Edit / Accept</li>
          </ol>
          <section className="decision-stage">
            <figure className="decision-image">
              {proposal.candidate?.preview_url && (
                <img
                  src={`${API_URL}${proposal.candidate.preview_url}`}
                  alt="Proposed Qlob Community post"
                />
              )}
              <figcaption>
                <span>Look {String(proposal.id).padStart(3, "0")}</span>
                <span>{topic?.franchise || "Visual candidate"}</span>
              </figcaption>
            </figure>

            <section className="decision-console" aria-label="Editorial decision">
              <div className="slot-cue">
                <span>Accept sends to Lineup</span>
                <strong>{nextSlot}</strong>
              </div>

              <label htmlFor="editorial-caption">Primary caption</label>
              <textarea
                ref={captionRef}
                id="editorial-caption"
                className={editing ? "caption-editing" : ""}
                value={caption}
                onChange={(event) => {
                  setCaption(event.target.value);
                  setEditing(true);
                }}
                onFocus={() => setEditing(true)}
                rows={4}
                maxLength={1000}
                disabled={busy !== null || decisionUncertain}
                spellCheck
                autoCapitalize="sentences"
              />
              <div className="caption-meta">
                <span>{editing ? "Editing live" : "Ready to refine"}</span>
                <span>{caption.length} / 1000</span>
              </div>

              <div className="decision-actions" aria-label="Decision controls">
                <button
                  type="button"
                  className="decision-reject"
                  disabled={busy !== null || decisionUncertain}
                  onClick={reject}
                  aria-busy={busy === "reject"}
                >
                  {busy === "reject" ? "Rejecting…" : "Reject"}
                </button>
                <button
                  type="button"
                  className={editing ? "decision-edit active" : "decision-edit"}
                  disabled={busy !== null || decisionUncertain}
                  onClick={toggleEdit}
                  aria-pressed={editing}
                >
                  Edit
                </button>
                <button
                  type="button"
                  className="decision-approve"
                  disabled={
                    busy !== null || decisionUncertain || !caption.trim()
                  }
                  onClick={accept}
                  aria-busy={busy === "accept"}
                >
                  {busy === "accept" ? "Accepting…" : "Accept"}
                </button>
              </div>

              <p
                className={messageIsError ? "decision-message error" : "decision-message"}
                role={messageIsError ? "alert" : "status"}
                aria-live={messageIsError ? "assertive" : "polite"}
              >
                {message ||
                  (publishingEnabled
                    ? "Accept schedules this exact image and caption on Qlob, then advances."
                    : "Accept reserves the next daily slot; YouTube scheduling is off.")}
              </p>
              {decisionUncertain && (
                <a className="decision-recovery" href="/review">
                  Reload Generator to verify
                </a>
              )}

              <div className="generator-tools" aria-label="Regenerate this option">
                <button
                  type="button"
                  disabled={busy !== null || decisionUncertain}
                  onClick={replaceImage}
                  aria-busy={busy === "replace"}
                >
                  {busy === "replace" ? "Replacing image…" : "Replace image"}
                </button>
                <button
                  type="button"
                  disabled={busy !== null || decisionUncertain}
                  onClick={regenerateCaptions}
                  aria-busy={busy === "regenerate"}
                >
                  {busy === "regenerate"
                    ? "Generating captions…"
                    : "Regenerate captions"}
                </button>
              </div>

              {proposal.alternative_captions.length > 0 && (
                <section className="caption-drawer caption-drawer-open">
                  <div className="caption-drawer-heading">
                    <strong>Alternative captions</strong>
                    <span>Choose one to edit or accept</span>
                  </div>
                  <div className="caption-options" aria-label="Caption options">
                    {proposal.alternative_captions.map((alternative, index) => (
                      <button
                        key={`${index}-${alternative}`}
                        type="button"
                        disabled={busy !== null || decisionUncertain}
                        className={caption === alternative ? "selected" : ""}
                        onClick={() => {
                          setCaption(alternative);
                          setEditing(true);
                        }}
                      >
                        {alternative}
                      </button>
                    ))}
                  </div>
                </section>
              )}
            </section>
          </section>

          <details className="editorial-context">
            <summary>Why this made the runway</summary>
            <div>
              <p>{proposal.caption_rationale || proposal.selection_reason}</p>
              <span>
                {proposal.caption_confidence === null
                  ? "Confidence not reported"
                  : `${Math.round(proposal.caption_confidence * 100)}% caption confidence`}
              </span>
              {topic?.characters?.length ? (
                <span>{topic.characters.join(", ")}</span>
              ) : null}
            </div>
          </details>
        </>
      ) : (
        <section className="editorial-empty">
          <span className="empty-counter">{sessionDecisions}</span>
          <h2>{sessionDecisions ? "That’s the edit." : "Bring in the first look."}</h2>
          <p>
            RunWay uses unused ranked images first, then opens a visible discovery pass for
            fresh material.
          </p>
          <button
            type="button"
            className="button"
            disabled={busy !== null}
            onClick={ensureOptions}
            aria-busy={busy === "options"}
          >
            {busy === "options" ? "Preparing looks…" : "Load more options"}
          </button>
          <small
            role={messageIsError ? "alert" : "status"}
            aria-live={messageIsError ? "assertive" : "polite"}
          >
            {message}
          </small>
        </section>
      )}
    </main>
  );
}
