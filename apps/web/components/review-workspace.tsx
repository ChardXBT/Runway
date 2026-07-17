"use client";
/* eslint-disable @next/next/no-img-element */

import { FormEvent, useState } from "react";

import { API_URL } from "@/lib/api";
import type { Proposal, PublishPreparation } from "@/lib/types";

const proposalDateFormatter = new Intl.DateTimeFormat("en-US", {
  weekday: "long",
  month: "long",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
  timeZone: "America/Toronto",
});

const historicalDateFormatter = new Intl.DateTimeFormat("en-US", {
  year: "numeric",
  month: "short",
  day: "numeric",
  timeZone: "America/Toronto",
});

const feedbackReasons = [
  ["prefer_open_question", "Prefer an open question"],
  ["too_generic", "Too generic"],
  ["not_engaging", "Not engaging"],
  ["wrong_emotion", "Wrong emotion"],
  ["wrong_character", "Wrong character"],
  ["invented_context", "Invented context"],
  ["not_funny", "Not funny enough"],
  ["too_long", "Too long"],
] as const;

function Percent({ value }: { value: number }) {
  return <>{Math.round(value * 100)}%</>;
}

function structureFor(caption: string) {
  return caption.trim().endsWith("?") ? "open_question" : "observation";
}

export function ReviewWorkspace({
  initialProposal,
  publishingEnabled = false,
}: {
  initialProposal: Proposal;
  publishingEnabled?: boolean;
}) {
  const [proposal, setProposal] = useState(initialProposal);
  const [caption, setCaption] = useState(initialProposal.final_caption);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [reasonCodes, setReasonCodes] = useState<string[]>([]);
  const [feedbackNote, setFeedbackNote] = useState("");
  const [imageVerdict, setImageVerdict] = useState("unsure");
  const [preparation, setPreparation] = useState<PublishPreparation | null>(null);
  const [confirmationPhrase, setConfirmationPhrase] = useState("");
  const [newDate, setNewDate] = useState(
    new Date(initialProposal.planned_publish_at).toISOString().slice(0, 10),
  );

  async function request(path: string, body?: object, method = "POST") {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`${API_URL}/api/proposals/${proposal.id}${path}`, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Action failed");
      setProposal(payload as Proposal);
      setCaption((payload as Proposal).final_caption);
      setMessage("Saved.");
      return payload as Proposal;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Action failed");
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function saveCaption(event: FormEvent) {
    event.preventDefault();
    await request(
      "",
      {
        final_caption: caption,
        reason_codes: reasonCodes,
        note: feedbackNote || null,
        image_verdict: imageVerdict,
      },
      "PATCH",
    );
  }

  async function savePreferred() {
    const saved = await request("/feedback", {
      verdict: "preferred",
      preferred_caption: caption,
      preferred_structure: structureFor(caption),
      reason_codes: reasonCodes,
      image_verdict: imageVerdict,
      note: feedbackNote || null,
    });
    if (saved) setMessage("Preference saved for future captions.");
  }

  async function rejectProposal() {
    await request("/reject", {
      reason: feedbackNote || "Caption or image was not a fit.",
      reason_codes: reasonCodes,
      image_verdict: imageVerdict,
    });
  }

  async function nextReview() {
    setBusy(true);
    const response = await fetch(`${API_URL}/api/proposals?status=needs_review&limit=100`);
    const rows = (await response.json()) as Proposal[];
    const next = rows.find((row) => row.id !== proposal.id) ?? rows[0];
    if (next) {
      setProposal(next);
      setCaption(next.final_caption);
      setNewDate(new Date(next.planned_publish_at).toISOString().slice(0, 10));
      setReasonCodes([]);
      setFeedbackNote("");
      setPreparation(null);
      setConfirmationPhrase("");
      setMessage("");
    } else {
      setMessage("No other proposals need review.");
    }
    setBusy(false);
  }

  function toggleReason(reason: string) {
    setReasonCodes((current) =>
      current.includes(reason)
        ? current.filter((value) => value !== reason)
        : [...current, reason],
    );
  }

  async function prepareYouTubeSchedule() {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(
        `${API_URL}/api/proposals/${proposal.id}/youtube/prepare`,
        { method: "POST" },
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Preparation failed");
      setPreparation(payload as PublishPreparation);
      setConfirmationPhrase("");
      setMessage("Prepared locally. Nothing has been submitted.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Preparation failed");
    } finally {
      setBusy(false);
    }
  }

  async function confirmYouTubeSchedule() {
    if (!preparation) return;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(
        `${API_URL}/api/publisher/attempts/${preparation.attempt_id}/confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            confirmation_token: preparation.confirmation_token,
            confirmation_phrase: confirmationPhrase,
          }),
        },
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "YouTube scheduling failed");
      setProposal(payload.proposal as Proposal);
      setCaption((payload.proposal as Proposal).final_caption);
      setPreparation(null);
      setConfirmationPhrase("");
      setMessage(payload.result.detail);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "YouTube scheduling failed");
    } finally {
      setBusy(false);
    }
  }

  async function verifyYouTubeSchedule() {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(
        `${API_URL}/api/proposals/${proposal.id}/youtube/verify`,
        { method: "POST" },
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Verification failed");
      setProposal(payload.proposal as Proposal);
      setMessage(payload.result.detail);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Verification failed");
    } finally {
      setBusy(false);
    }
  }

  const topic = proposal.candidate?.detected_topic ?? {};
  const verifiedRights = ["creator_owned", "licensed", "public_domain"];
  const rightsReady =
    verifiedRights.includes(proposal.candidate?.rights_status ?? "") ||
    proposal.rights_decision === "accepted_for_proposal";

  return (
    <div className="review-workspace">
      <header className="review-header">
        <div>
          <p className="eyebrow">Proposal #{proposal.id}</p>
          <h1>{proposalDateFormatter.format(new Date(proposal.planned_publish_at))}</h1>
        </div>
        <span className={`status status-${proposal.status}`}>
          {proposal.status.replaceAll("_", " ")}
        </span>
      </header>

      <section className="review-grid">
        <div className="review-visuals">
          <div className="original-frame">
            {proposal.candidate?.original_url && (
              <img
                src={`${API_URL}${proposal.candidate.original_url}`}
                alt="Candidate original"
              />
            )}
            <span>Original</span>
          </div>
          <div className="preview-frame">
            {proposal.candidate?.preview_url && (
              <img
                src={`${API_URL}${proposal.candidate.preview_url}`}
                alt="Square feed preview"
              />
            )}
            <span>1:1 feed preview</span>
          </div>
        </div>

        <div className="review-editor">
          <form onSubmit={saveCaption}>
            <label htmlFor="final-caption">Final caption</label>
            <textarea
              id="final-caption"
              rows={6}
              value={caption}
              onChange={(event) => setCaption(event.target.value)}
            />
            <div className="form-actions">
              <button className="button secondary" disabled={busy} type="submit">
                Save caption
              </button>
              <small>{caption.length} characters</small>
            </div>
          </form>

          <div className="alternatives">
            <p className="eyebrow">Structural alternatives</p>
            {proposal.alternative_captions.map((alternative, index) => (
              <button
                key={alternative}
                disabled={busy}
                onClick={() => request("/select-alternative", { index })}
              >
                {alternative}
              </button>
            ))}
          </div>

          <section className="learning-strip" aria-labelledby="learning-title">
            <div>
              <p className="eyebrow">Teach LeeWay</p>
              <h2 id="learning-title">Turn this decision into the next caption.</h2>
              <p>
                Saved preferences are retrieved immediately. Open questions remain the primary
                engagement goal.
              </p>
            </div>
            <div className="feedback-reasons">
              {feedbackReasons.map(([value, label]) => (
                <label key={value} className={reasonCodes.includes(value) ? "selected" : ""}>
                  <input
                    type="checkbox"
                    checked={reasonCodes.includes(value)}
                    onChange={() => toggleReason(value)}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>
            <div className="feedback-fields">
              <label>
                <span>Image</span>
                <select
                  value={imageVerdict}
                  onChange={(event) => setImageVerdict(event.target.value)}
                >
                  <option value="good">Good image</option>
                  <option value="unsure">Unsure</option>
                  <option value="bad">Bad image</option>
                </select>
              </label>
              <label>
                <span>Optional note</span>
                <input
                  value={feedbackNote}
                  onChange={(event) => setFeedbackNote(event.target.value)}
                  placeholder="Why did you change it?"
                />
              </label>
            </div>
            <button className="button secondary" disabled={busy} onClick={savePreferred}>
              Save as preferred
            </button>
          </section>

          <div className="caption-evidence">
            <p className="eyebrow">Caption grounding</p>
            <p>{proposal.caption_rationale || "No rationale recorded."}</p>
            <small>
              Confidence{" "}
              {proposal.caption_confidence === null
                ? "not reported"
                : `${Math.round(proposal.caption_confidence * 100)}%`}
              {" · "}
              References{" "}
              {proposal.caption_reference_post_ids.length
                ? proposal.caption_reference_post_ids.join(", ")
                : "none"}
            </small>
            {proposal.factual_uncertainty_warning && (
              <p className="caption-warning">{proposal.factual_uncertainty_warning}</p>
            )}
          </div>

          <div className="primary-actions">
            {proposal.status === "needs_review" && (
              <button
                className="button approve"
                disabled={busy || !rightsReady}
                title={rightsReady ? "Approve proposal" : "Review image provenance first"}
                onClick={() => request("/approve")}
              >
                Approve
              </button>
            )}
            {proposal.status === "needs_review" && (
              <button className="button reject" disabled={busy} onClick={rejectProposal}>
                Reject
              </button>
            )}
            {proposal.status === "approved" && (
              <button
                className="button"
                disabled={busy}
                onClick={() => request("/internal-schedule")}
              >
                Schedule internally
              </button>
            )}
            <button className="button secondary" disabled={busy} onClick={nextReview}>
              Next review
            </button>
          </div>
          <p className="action-status" role="status">
            {busy ? "Working…" : message}
          </p>
        </div>
      </section>

      {publishingEnabled &&
        ["internally_scheduled", "publish_failed"].includes(proposal.status) && (
          <section className="publisher-interlock panel">
            <div>
              <p className="eyebrow">External scheduling interlock</p>
              <h2>Prepare this exact proposal for Qlob.</h2>
              <p>
                Preparation validates the Editor session and creates a ten-minute token. It does
                not submit anything.
              </p>
            </div>
            <button
              className="button secondary"
              disabled={busy}
              onClick={prepareYouTubeSchedule}
            >
              Prepare YouTube schedule
            </button>
            {preparation && (
              <div className="publisher-confirmation">
                <dl>
                  <dt>Channel</dt>
                  <dd>{preparation.channel_name}</dd>
                  <dt>Caption</dt>
                  <dd>{preparation.caption}</dd>
                  <dt>Schedule</dt>
                  <dd>
                    {proposalDateFormatter.format(
                      new Date(preparation.planned_publish_at),
                    )}
                  </dd>
                  <dt>Expires</dt>
                  <dd>{new Date(preparation.expires_at).toLocaleTimeString()}</dd>
                </dl>
                <label htmlFor="schedule-confirmation">
                  Type <strong>{preparation.confirmation_phrase}</strong>
                </label>
                <input
                  id="schedule-confirmation"
                  value={confirmationPhrase}
                  onChange={(event) => setConfirmationPhrase(event.target.value)}
                  autoComplete="off"
                />
                <button
                  className="button reject"
                  disabled={
                    busy || confirmationPhrase.trim() !== preparation.confirmation_phrase
                  }
                  onClick={confirmYouTubeSchedule}
                >
                  Schedule on YouTube
                </button>
              </div>
            )}
          </section>
        )}

      {publishingEnabled &&
        ["publishing", "publish_unverified"].includes(proposal.status) && (
          <section className="publisher-interlock panel">
            <div>
              <p className="eyebrow">Submission needs verification</p>
              <h2>Do not submit again.</h2>
              <p>Check Qlob’s Scheduled tab before taking another action.</p>
            </div>
            <button
              className="button secondary"
              disabled={busy}
              onClick={verifyYouTubeSchedule}
            >
              Verify scheduled post
            </button>
          </section>
        )}

      {proposal.status === "externally_scheduled" && (
        <section className="publisher-receipt panel">
          <div>
            <p className="eyebrow">Externally scheduled</p>
            <h2>Qlob Scheduled-tab verification passed.</h2>
            <p>
              The exact caption, date, time, and an image thumbnail were observed after
              submission.
            </p>
          </div>
          {proposal.external_post_url && (
            <a
              className="button secondary"
              href={proposal.external_post_url}
              target="_blank"
              rel="noreferrer"
            >
              Open scheduled post
            </a>
          )}
        </section>
      )}

      <section className="score-strip" aria-label="Candidate scores">
        <div>
          <span>Style</span>
          <strong>
            <Percent value={proposal.scores.style} />
          </strong>
        </div>
        <div>
          <span>Novelty</span>
          <strong>
            <Percent value={proposal.scores.novelty} />
          </strong>
        </div>
        <div>
          <span>Quality</span>
          <strong>
            <Percent value={proposal.scores.quality} />
          </strong>
        </div>
        <div>
          <span>Detected show</span>
          <strong>{topic.franchise || "Unknown"}</strong>
        </div>
      </section>

      <section className="review-details">
        <article className="panel provenance-panel">
          <p className="eyebrow">Why this image</p>
          <h2>{proposal.selection_reason}</h2>
          <dl>
            <dt>Source</dt>
            <dd>{proposal.candidate?.source_domain || "Unknown"}</dd>
            <dt>Rights</dt>
            <dd>{proposal.candidate?.rights_status || "Unknown"}</dd>
            <dt>Review decision</dt>
            <dd>{proposal.rights_decision?.replaceAll("_", " ") || "Required"}</dd>
            <dt>Characters</dt>
            <dd>{topic.characters?.join(", ") || "Unverified"}</dd>
            <dt>Composition</dt>
            <dd>{topic.composition || "Unknown"}</dd>
          </dl>
          {proposal.candidate?.source_page_url && (
            <a href={proposal.candidate.source_page_url} target="_blank" rel="noreferrer">
              Open source page ↗
            </a>
          )}
          {proposal.candidate?.rights_status === "unknown" &&
            proposal.rights_decision !== "accepted_for_proposal" && (
              <div className="rights-gate">
                <strong>Human provenance decision required</strong>
                <p>
                  Opening the source and accepting it records your decision for this proposal;
                  it does not claim the image is licensed.
                </p>
                <button
                  className="button secondary"
                  disabled={busy}
                  onClick={() =>
                    request("/rights-review", { decision: "accepted_for_proposal" })
                  }
                >
                  I reviewed the source · accept
                </button>
              </div>
            )}
          {proposal.warnings.length > 0 && (
            <ul className="warning-list">
              {proposal.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
        </article>
        <article className="panel match-panel">
          <p className="eyebrow">Closest historical matches</p>
          {proposal.closest_historical_matches.slice(0, 5).map((match) => (
            <div key={match.post_id}>
              {match.media_url && <img src={`${API_URL}${match.media_url}`} alt="" />}
              <span>{Math.round(match.visual_similarity * 100)}%</span>
              <p>{match.caption}</p>
              <small>
                {match.published_at
                  ? historicalDateFormatter.format(new Date(match.published_at))
                  : "Date uncertain"}
              </small>
            </div>
          ))}
        </article>
      </section>

      {proposal.caption_feedback && proposal.caption_feedback.length > 0 && (
        <section className="panel feedback-history">
          <p className="eyebrow">Learning history</p>
          {proposal.caption_feedback.slice(-5).reverse().map((item) => (
            <article key={item.id}>
              <span>{item.verdict.replaceAll("_", " ")}</span>
              <strong>{item.preferred_caption || item.generated_caption}</strong>
              <small>
                {[item.preferred_structure, ...item.reason_codes]
                  .filter(Boolean)
                  .join(" · ")}
              </small>
            </article>
          ))}
        </section>
      )}

      <section className="panel secondary-actions">
        <button disabled={busy} onClick={() => request("/regenerate")}>
          Regenerate captions
        </button>
        <button disabled={busy} onClick={() => request("/replace", {})}>
          Show next candidate
        </button>
        <button disabled={busy} onClick={() => request("/block-image")}>
          Block image + replace
        </button>
        <button disabled={busy} onClick={() => request("/block-domain")}>
          Block domain + replace
        </button>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void request("/reschedule", { new_date: newDate });
          }}
        >
          <label htmlFor="move-date">Move date</label>
          <input
            id="move-date"
            type="date"
            value={newDate}
            onChange={(event) => setNewDate(event.target.value)}
          />
          <button disabled={busy} type="submit">
            Move
          </button>
        </form>
        <details className="metadata-editor">
          <summary>Correct detected metadata</summary>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              const data = new FormData(event.currentTarget);
              void request("/metadata", {
                fields: {
                  franchise: String(data.get("franchise") || ""),
                  characters: String(data.get("characters") || "")
                    .split(",")
                    .map((value) => value.trim())
                    .filter(Boolean),
                  composition: String(data.get("composition") || ""),
                },
              });
            }}
          >
            <input
              name="franchise"
              aria-label="Detected franchise"
              defaultValue={topic.franchise ?? ""}
            />
            <input
              name="characters"
              aria-label="Detected characters"
              defaultValue={topic.characters?.join(", ") ?? ""}
            />
            <input
              name="composition"
              aria-label="Detected composition"
              defaultValue={topic.composition ?? ""}
            />
            <button type="submit" disabled={busy}>
              Save metadata
            </button>
          </form>
        </details>
      </section>
    </div>
  );
}
