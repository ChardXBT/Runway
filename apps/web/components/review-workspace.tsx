"use client";
/* eslint-disable @next/next/no-img-element */

import { useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import type {
  EditorialEnvelope,
  Proposal,
  PublisherQueueStatus,
  WorkflowStatus,
} from "@/lib/types";

const slotFormatter = new Intl.DateTimeFormat("en-US", {
  weekday: "short",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
  timeZone: "America/Toronto",
});

type Action = "approve" | "reject" | "image" | "options" | "resume";

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
  const [message, setMessage] = useState("");
  const [sessionDecisions, setSessionDecisions] = useState(0);
  const warmingTray = useRef(false);

  function showProposal(next: Proposal | null) {
    setProposal(next);
    setCaption(next?.final_caption ?? "");
  }

  async function parseResponse(response: Response) {
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "LeeWay could not complete that decision.");
    }
    return payload as EditorialEnvelope;
  }

  async function ensureOptions() {
    setBusy("options");
    setMessage("Finding and ranking the next options…");
    try {
      const response = await fetch(`${API_URL}/api/editorial/options/ensure`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: 5, live_discovery: true }),
      });
      const payload = await parseResponse(response);
      setWorkflow(payload.workflow);
      showProposal(payload.next_proposal);
      setMessage(
        payload.next_proposal
          ? "The next option is ready."
          : payload.detail || "No accepted options were found.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not find more options.");
    } finally {
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
      const payload = await parseResponse(response);
      setWorkflow(payload.workflow);
    } catch {
      // Empty-tray refill remains the visible recovery path.
    } finally {
      warmingTray.current = false;
    }
  }

  async function finishDecision(payload: EditorialEnvelope, notice: string) {
    setWorkflow(payload.workflow);
    if (payload.publisher_queue) setPublisherQueue(payload.publisher_queue);
    setSessionDecisions((value) => value + 1);
    setMessage(notice);
    if (payload.next_proposal) {
      showProposal(payload.next_proposal);
      if (payload.workflow.needs_review <= 2) void warmTray();
      return;
    }
    showProposal(null);
    await ensureOptions();
  }

  async function approve() {
    if (!proposal || busy) return;
    setBusy("approve");
    setMessage("");
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
          ? `Approved for ${slotFormatter.format(new Date(scheduledAt))}.`
          : "Approved and added to the scheduling queue.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Approval failed.");
    } finally {
      setBusy(null);
    }
  }

  async function reject() {
    if (!proposal || busy) return;
    setBusy("reject");
    setMessage("");
    try {
      const response = await fetch(
        `${API_URL}/api/editorial/proposals/${proposal.id}/reject`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "The complete option was not a fit." }),
        },
      );
      await finishDecision(await parseResponse(response), "Rejected. LeeWay will avoid this fit.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Rejection failed.");
    } finally {
      setBusy(null);
    }
  }

  async function anotherImage() {
    if (!proposal || busy) return;
    setBusy("image");
    setMessage("");
    try {
      const response = await fetch(
        `${API_URL}/api/editorial/proposals/${proposal.id}/skip-image`,
        { method: "POST" },
      );
      const payload = await parseResponse(response);
      await finishDecision(payload, "Image rejected. Its negative signal is saved.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "No replacement image is ready.");
    } finally {
      setBusy(null);
    }
  }

  async function resumePublisher() {
    setBusy("resume");
    try {
      const response = await fetch(`${API_URL}/api/publisher/queue/resume`, {
        method: "POST",
      });
      const payload = (await response.json()) as PublisherQueueStatus;
      if (!response.ok) throw new Error("The publisher queue could not resume.");
      setPublisherQueue(payload);
      setMessage("YouTube scheduling resumed.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not resume scheduling.");
    } finally {
      setBusy(null);
    }
  }

  const nextSlot = slotFormatter.format(new Date(workflow.next_available_at));
  const topic = proposal?.candidate?.detected_topic;

  return (
    <main className="editorial-conveyor">
      <header className="conveyor-header">
        <div>
          <p className="eyebrow">Qlob editorial feed</p>
          <h1>{proposal ? "Make the call." : "Your tray is clear."}</h1>
        </div>
        <div className="conveyor-stats" aria-label="Editorial session status">
          <span>
            <strong>{sessionDecisions}</strong> this session
          </span>
          <span>
            <strong>{workflow.queued}</strong> on the way
          </span>
          <span>
            <strong>{workflow.scheduled}</strong> scheduled
          </span>
        </div>
      </header>

      {publisherQueue.paused && (
        <section className="queue-pause" role="alert">
          <div>
            <strong>Scheduling paused safely.</strong>
            <span>{publisherQueue.paused_reason}</span>
          </div>
          <button
            className="button secondary"
            disabled={busy !== null}
            onClick={resumePublisher}
          >
            Resume
          </button>
        </section>
      )}

      {proposal ? (
        <>
          <section className="decision-stage">
            <figure className="decision-image">
              {proposal.candidate?.preview_url && (
                <img
                  src={`${API_URL}${proposal.candidate.preview_url}`}
                  alt="Proposed Qlob Community post"
                />
              )}
              <figcaption>
                Option {proposal.id}
                <span>{topic?.franchise || "Visual candidate"}</span>
              </figcaption>
            </figure>

            <section className="decision-console" aria-label="Editorial decision">
              <div className="slot-cue">
                <span>Next open bot slot</span>
                <strong>{nextSlot}</strong>
              </div>

              <label htmlFor="editorial-caption">Caption</label>
              <textarea
                id="editorial-caption"
                value={caption}
                onChange={(event) => setCaption(event.target.value)}
                rows={4}
                maxLength={1000}
                autoFocus
              />

              {proposal.alternative_captions.length > 0 && (
                <div className="caption-options" aria-label="Caption options">
                  {proposal.alternative_captions.map((alternative) => (
                    <button
                      key={alternative}
                      type="button"
                      disabled={busy !== null}
                      className={caption === alternative ? "selected" : ""}
                      onClick={() => setCaption(alternative)}
                    >
                      {alternative}
                    </button>
                  ))}
                </div>
              )}

              <div className="decision-actions">
                <button
                  className="decision-reject"
                  disabled={busy !== null}
                  onClick={reject}
                >
                  {busy === "reject" ? "Rejecting…" : "Reject"}
                </button>
                <button
                  className="decision-next"
                  disabled={busy !== null}
                  onClick={anotherImage}
                >
                  {busy === "image" ? "Changing…" : "Another image"}
                </button>
                <button
                  className="decision-approve"
                  disabled={busy !== null || !caption.trim()}
                  onClick={approve}
                >
                  {busy === "approve" ? "Queuing…" : "Approve & schedule"}
                </button>
              </div>

              <p className="decision-message" role="status">
                {message ||
                  (publishingEnabled
                    ? "Approval schedules this post on Qlob and opens the next option."
                    : "Approval reserves the next daily slot; YouTube publishing is currently off.")}
              </p>
            </section>
          </section>

          <details className="editorial-context">
            <summary>Why LeeWay chose this option</summary>
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
          <h2>{sessionDecisions ? "Good run." : "Feed the tray."}</h2>
          <p>
            LeeWay will use unused ranked images first, then open a visible discovery pass when
            it needs fresh material.
          </p>
          <button
            className="button"
            disabled={busy !== null}
            onClick={ensureOptions}
          >
            {busy === "options" ? "Finding options…" : "Find more options"}
          </button>
          <small role="status">{message}</small>
        </section>
      )}
    </main>
  );
}
