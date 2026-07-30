"use client";
/* eslint-disable @next/next/no-img-element */

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import {
  actionError,
  hasUncertainOutcome,
  readApiJson,
} from "@/lib/client-api";
import {
  isEditorialEnvelope,
  isProposal,
} from "@/lib/guards";
import type {
  EditorialEnvelope,
  GenerationActivity,
  Proposal,
  WorkflowStatus,
} from "@/lib/types";

type Action =
  | "accept"
  | "reject"
  | "similar"
  | "options"
  | "regenerate"
  | "replace";

function emptyGeneration(): GenerationActivity {
  return {
    running: false,
    started_at: null,
    completed_at: null,
    detail: null,
  };
}

export function ReviewWorkspace({
  initialProposal,
  initialWorkflow,
  initialGeneration,
}: {
  initialProposal: Proposal | null;
  initialWorkflow: WorkflowStatus;
  initialGeneration?: GenerationActivity;
}) {
  const [proposal, setProposal] = useState(initialProposal);
  const [caption, setCaption] = useState(initialProposal?.final_caption ?? "");
  const [workflow, setWorkflow] = useState(initialWorkflow);
  const [generation, setGeneration] = useState(
    initialGeneration ?? emptyGeneration(),
  );
  const [busy, setBusy] = useState<Action | null>(null);
  const [editing, setEditing] = useState(false);
  const [message, setMessage] = useState("");
  const [messageIsError, setMessageIsError] = useState(false);
  const [decisionUncertain, setDecisionUncertain] = useState(false);
  const [loadedImageId, setLoadedImageId] = useState<number | null>(null);
  const [failedImageId, setFailedImageId] = useState<number | null>(null);
  const [sessionDecisions, setSessionDecisions] = useState(0);
  const warmingTray = useRef(false);
  const actionLock = useRef(false);
  const captionRef = useRef<HTMLTextAreaElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
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
    setLoadedImageId(null);
    setFailedImageId(null);
  }

  function applyEditorialStatus(payload: EditorialEnvelope) {
    setWorkflow(payload.workflow);
    setGeneration(payload.generation ?? emptyGeneration());
  }

  async function parseResponse(response: Response) {
    return readApiJson(response, {
      validate: isEditorialEnvelope,
      failureMessage: "Runway could not complete that decision.",
    });
  }

  async function parseProposalResponse(response: Response) {
    return readApiJson(response, {
      validate: isProposal,
      failureMessage: "Runway could not refresh this option.",
    });
  }

  async function requestOptions() {
    setGeneration((current) => ({
      ...current,
      running: true,
      started_at: current.started_at ?? new Date().toISOString(),
      completed_at: null,
      detail: "Discovering images and generating captions.",
    }));
    const response = await fetch(`${API_URL}/api/editorial/options/ensure`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: 1, live_discovery: true }),
    });
    const payload = await parseResponse(response);
    applyEditorialStatus(payload);
    showProposal(payload.next_proposal);
    setNotice(
      payload.next_proposal
        ? "The next option is ready."
        : payload.generation?.running
          ? payload.generation.detail ||
            "Generation is running. Runway will show the option when it is ready."
          : payload.generation?.detail ||
            payload.detail ||
            "No usable options were found.",
    );
    if (payload.next_proposal && payload.workflow.needs_review <= 2) {
      void warmTray();
    }
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
        body: JSON.stringify({ target: 5, live_discovery: false }),
      });
      await parseResponse(response);
    } catch {
      // Empty-runway refill remains the visible recovery path.
    } finally {
      warmingTray.current = false;
    }
  }

  async function finishDecision(payload: EditorialEnvelope, notice: string) {
    applyEditorialStatus(payload);
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
      !caption.trim() ||
      loadedImageId !== proposal.candidate_image_id
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
          ? `Accepted and added to Lineup for ${
              slotFormatter?.format(new Date(scheduledAt)) ??
              "the configured Lineup slot"
            }. Nothing was sent to YouTube.`
          : "Accepted and added to Lineup. Nothing was sent to YouTube.",
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
          body: JSON.stringify({
            reason: "The complete option was not a fit.",
            reason_codes: ["not_engaging"],
          }),
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

  async function showFewerLikeThis() {
    if (!proposal || actionLock.current || decisionUncertain) return;
    actionLock.current = true;
    setBusy("similar");
    setNotice("");
    try {
      const response = await fetch(
        `${API_URL}/api/editorial/proposals/${proposal.id}/reject`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reason:
              "This image is too similar to recent options. Show fewer images from this visual cluster.",
            reason_codes: ["too_similar"],
          }),
        },
      );
      await finishDecision(
        await parseResponse(response),
        "Hidden. Similar images will receive a negative preference signal.",
      );
    } catch (error) {
      const uncertain = hasUncertainOutcome(error);
      setDecisionUncertain(uncertain);
      setNotice(
        actionError(
          error,
          "The similarity feedback failed. This option is still on screen.",
          "The similarity response could not be verified. Reload Generator before making another decision.",
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
      const uncertain = hasUncertainOutcome(error);
      setDecisionUncertain(uncertain);
      setNotice(
        actionError(
          error,
          "Caption regeneration failed. The current caption is preserved.",
          "The regeneration response could not be verified. Reload Generator to reconcile the current captions before trying again.",
        ),
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
      const uncertain = hasUncertainOutcome(error);
      setDecisionUncertain(uncertain);
      setNotice(
        actionError(
          error,
          "Image replacement failed. The current option is preserved.",
          "The image replacement response could not be verified. Reload Generator to reconcile the current image before trying again.",
        ),
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

  useEffect(() => {
    const image = imageRef.current;
    const candidateImageId = proposal?.candidate_image_id;
    if (
      image &&
      candidateImageId !== undefined &&
      image.complete &&
      image.naturalWidth > 0
    ) {
      // A cached image may finish before React hydrates and attaches onLoad.
      // Reconcile the DOM state so a visibly loaded image cannot leave Accept
      // disabled forever.
      setLoadedImageId(candidateImageId);
      setFailedImageId((current) =>
        current === candidateImageId ? null : current,
      );
    }
  }, [proposal?.candidate_image_id, proposal?.candidate?.preview_url]);

  useEffect(() => {
    if (proposal || !generation.running || busy === "options") return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let controller: AbortController | null = null;
    let nextDelay = 3000;

    async function pollGeneration() {
      let keepPolling = true;
      controller = new AbortController();
      try {
        const response = await fetch(`${API_URL}/api/editorial/next`, {
          cache: "no-store",
          signal: controller.signal,
        });
        const payload = await readApiJson(response, {
          validate: isEditorialEnvelope,
          failureMessage: "Runway could not check generation progress.",
        });
        if (cancelled) return;
        applyEditorialStatus(payload);
        if (payload.next_proposal) {
          showProposal(payload.next_proposal);
          setNotice("The generated option is ready.");
          keepPolling = false;
        } else if (payload.generation?.running) {
          setNotice(
            payload.generation.detail ||
              "Discovering images and generating captions. This can take a few minutes.",
          );
        } else {
          setNotice(
            payload.generation?.detail ||
              "Generation finished without a usable image. Try Generate more.",
            true,
          );
          keepPolling = false;
        }
        nextDelay = 3000;
      } catch (error) {
        if (cancelled || controller.signal.aborted) return;
        nextDelay = Math.min(nextDelay * 2, 15_000);
        setNotice(
          actionError(
            error,
            "Generation may still be running, but Runway could not check its progress.",
          ),
          true,
        );
      }
      if (!cancelled && keepPolling) {
        timer = setTimeout(pollGeneration, nextDelay);
      }
    }

    void pollGeneration();
    return () => {
      cancelled = true;
      controller?.abort();
      if (timer) clearTimeout(timer);
    };
  }, [busy, generation.running, proposal]);

  const nextSlot =
    slotFormatter?.format(new Date(workflow.next_available_at)) ??
    "Next configured opening";
  const topic = proposal?.candidate?.detected_topic;

  return (
    <div className="editorial-conveyor">
      <header className="conveyor-header">
        <div>
          <p className="eyebrow">Qlob Generator</p>
          <h1>
            {proposal
                ? "Choose the next post"
              : generation.running
                ? "Generator is working"
                : generation.detail
                  ? "Ready for another search"
                  : "Generator ready"}
          </h1>
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
              {proposal.candidate?.preview_url ? (
                <img
                  ref={imageRef}
                  key={proposal.candidate_image_id}
                  src={`${API_URL}${proposal.candidate.preview_url}`}
                  alt={
                    topic?.franchise
                      ? `Proposed ${topic.franchise} image for a Qlob Community post`
                      : "Proposed image for a Qlob Community post"
                  }
                  onLoad={() => {
                    setLoadedImageId(proposal.candidate_image_id);
                    setFailedImageId((current) =>
                      current === proposal.candidate_image_id ? null : current,
                    );
                  }}
                  onError={() => {
                    setFailedImageId(proposal.candidate_image_id);
                    setLoadedImageId((current) =>
                      current === proposal.candidate_image_id ? null : current,
                    );
                  }}
                />
              ) : (
                <div className="decision-image-missing" role="alert">
                  This option has no renderable preview. Replace or reject the image before
                  accepting it.
                </div>
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
                readOnly={!editing}
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
                    busy !== null ||
                    decisionUncertain ||
                    !caption.trim() ||
                    loadedImageId !== proposal.candidate_image_id
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
                  (failedImageId === proposal.candidate_image_id
                    ? "The exact image could not be loaded. Retry the media, replace the image, or reject this option."
                    : loadedImageId !== proposal.candidate_image_id
                      ? "Loading the exact image before acceptance is enabled."
                      : "Accept records the editorial decision and adds this exact image and caption to Lineup. It never opens or queues YouTube.")}
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
                  onClick={showFewerLikeThis}
                  aria-busy={busy === "similar"}
                >
                  {busy === "similar" ? "Saving preference…" : "Fewer like this"}
                </button>
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
                <details className="caption-drawer">
                  <summary>
                    <strong>Alternative captions</strong>
                    <span>{proposal.alternative_captions.length} options</span>
                  </summary>
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
                </details>
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
        <section
          className={generation.running ? "editorial-empty generation-active" : "editorial-empty"}
        >
          <span className="empty-counter" aria-hidden="true">
            {generation.running ? <span className="generation-pulse" /> : sessionDecisions}
          </span>
          <h2>
            {generation.running
              ? "Building the next look."
              : sessionDecisions
                ? "That’s the edit."
                : generation.detail
                  ? "Choose the next move."
                  : "Bring in the first look."}
          </h2>
          <p>
            {generation.running
              ? "Runway is searching for a usable image, checking it, and asking Codex for captions. You can visit another section and come back."
              : generation.detail
                ? "The previous pass finished or paused. Generate more starts a fresh search while preserving every decision you already made."
                : "Runway uses unused ranked images first, then opens a visible discovery pass for fresh material."}
          </p>
          <div className="empty-actions">
            <button
              type="button"
              className="button"
              disabled={busy !== null || generation.running}
              onClick={ensureOptions}
              aria-busy={busy === "options"}
            >
              {generation.running
                ? "Generating…"
                : busy === "options"
                  ? "Starting generation…"
                  : "Generate more"}
            </button>
            {!generation.running && generation.detail && (
              <Link className="button secondary" href="/catalogue">
                Review archive
              </Link>
            )}
          </div>
          <small
            id="generation-status"
            role={messageIsError ? "alert" : "status"}
            aria-live={messageIsError ? "assertive" : "polite"}
          >
            {message ||
              (generation.running
                ? "Generation usually takes a few minutes."
                : generation.detail)}
          </small>
        </section>
      )}
    </div>
  );
}
