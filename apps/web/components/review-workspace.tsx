"use client";
/* eslint-disable @next/next/no-img-element */

import { FormEvent, useState } from "react";

import { API_URL } from "@/lib/api";
import type { Proposal } from "@/lib/types";

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

function Percent({ value }: { value: number }) {
  return <>{Math.round(value * 100)}%</>;
}

export function ReviewWorkspace({ initialProposal }: { initialProposal: Proposal }) {
  const [proposal, setProposal] = useState(initialProposal);
  const [caption, setCaption] = useState(initialProposal.final_caption);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
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
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  async function saveCaption(event: FormEvent) {
    event.preventDefault();
    await request("", { final_caption: caption }, "PATCH");
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
      setMessage("");
    } else {
      setMessage("No other proposals need review.");
    }
    setBusy(false);
  }

  const topic = proposal.candidate?.detected_topic ?? {};
  return (
    <div className="review-workspace">
      <header className="review-header">
        <div>
          <p className="eyebrow">Proposal #{proposal.id}</p>
          <h1>{proposalDateFormatter.format(new Date(proposal.planned_publish_at))}</h1>
        </div>
        <span className={`status status-${proposal.status}`}>{proposal.status.replaceAll("_", " ")}</span>
      </header>

      <section className="review-grid">
        <div className="review-visuals">
          <div className="original-frame">
            {proposal.candidate?.original_url && (
              <img src={`${API_URL}${proposal.candidate.original_url}`} alt="Candidate original" />
            )}
            <span>Original</span>
          </div>
          <div className="preview-frame">
            {proposal.candidate?.preview_url && (
              <img src={`${API_URL}${proposal.candidate.preview_url}`} alt="Square feed preview" />
            )}
            <span>1:1 feed preview</span>
          </div>
        </div>

        <div className="review-editor">
          <form onSubmit={saveCaption}>
            <label htmlFor="final-caption">Final caption</label>
            <textarea id="final-caption" rows={6} value={caption} onChange={(event) => setCaption(event.target.value)} />
            <div className="form-actions">
              <button className="button secondary" disabled={busy} type="submit">Save caption</button>
              <small>{caption.length} characters</small>
            </div>
          </form>
          <div className="alternatives">
            <p className="eyebrow">Alternatives</p>
            {proposal.alternative_captions.map((alternative, index) => (
              <button key={alternative} disabled={busy} onClick={() => request("/select-alternative", { index })}>
                {alternative}
              </button>
            ))}
          </div>
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
            {proposal.status === "needs_review" && <button className="button approve" disabled={busy} onClick={() => request("/approve")}>Approve</button>}
            {proposal.status === "needs_review" && <button className="button reject" disabled={busy} onClick={() => request("/reject", { reason: "not a fit" })}>Reject</button>}
            {proposal.status === "approved" && <button className="button" disabled={busy} onClick={() => request("/internal-schedule")}>Schedule internally</button>}
            <button className="button secondary" disabled={busy} onClick={nextReview}>Next review</button>
          </div>
          <p className="action-status" role="status">{busy ? "Working…" : message}</p>
        </div>
      </section>

      <section className="score-strip" aria-label="Candidate scores">
        <div><span>Style</span><strong><Percent value={proposal.scores.style} /></strong></div>
        <div><span>Novelty</span><strong><Percent value={proposal.scores.novelty} /></strong></div>
        <div><span>Quality</span><strong><Percent value={proposal.scores.quality} /></strong></div>
        <div><span>Detected show</span><strong>{topic.franchise || "Unknown"}</strong></div>
      </section>

      <section className="review-details">
        <article className="panel provenance-panel">
          <p className="eyebrow">Why this image</p>
          <h2>{proposal.selection_reason}</h2>
          <dl>
            <dt>Source</dt><dd>{proposal.candidate?.source_domain || "Unknown"}</dd>
            <dt>Rights</dt><dd>{proposal.candidate?.rights_status || "Unknown"}</dd>
            <dt>Characters</dt><dd>{topic.characters?.join(", ") || "Unverified"}</dd>
            <dt>Composition</dt><dd>{topic.composition || "Unknown"}</dd>
          </dl>
          {proposal.candidate?.source_page_url && <a href={proposal.candidate.source_page_url} target="_blank" rel="noreferrer">Open source page ↗</a>}
          {proposal.warnings.length > 0 && <ul className="warning-list">{proposal.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
        </article>
        <article className="panel match-panel">
          <p className="eyebrow">Closest historical matches</p>
          {proposal.closest_historical_matches.slice(0, 5).map((match) => (
            <div key={match.post_id}>{match.media_url && <img src={`${API_URL}${match.media_url}`} alt="" />}<span>{Math.round(match.visual_similarity * 100)}%</span><p>{match.caption}</p><small>{match.published_at ? historicalDateFormatter.format(new Date(match.published_at)) : "Date uncertain"}</small></div>
          ))}
        </article>
      </section>

      <section className="panel secondary-actions">
        <button disabled={busy} onClick={() => request("/regenerate")}>Regenerate captions</button>
        <button disabled={busy} onClick={() => request("/replace", {})}>Show next candidate</button>
        <button disabled={busy} onClick={() => request("/block-image")}>Block image + replace</button>
        <button disabled={busy} onClick={() => request("/block-domain")}>Block domain + replace</button>
        <form onSubmit={(event) => { event.preventDefault(); void request("/reschedule", { new_date: newDate }); }}>
          <label htmlFor="move-date">Move date</label>
          <input id="move-date" type="date" value={newDate} onChange={(event) => setNewDate(event.target.value)} />
          <button disabled={busy} type="submit">Move</button>
        </form>
        <details className="metadata-editor">
          <summary>Correct detected metadata</summary>
          <form onSubmit={(event) => { event.preventDefault(); const data = new FormData(event.currentTarget); void request("/metadata", { fields: { franchise: String(data.get("franchise") || ""), characters: String(data.get("characters") || "").split(",").map((value) => value.trim()).filter(Boolean), composition: String(data.get("composition") || "") } }); }}>
            <input name="franchise" aria-label="Detected franchise" defaultValue={topic.franchise ?? ""} />
            <input name="characters" aria-label="Detected characters" defaultValue={topic.characters?.join(", ") ?? ""} />
            <input name="composition" aria-label="Detected composition" defaultValue={topic.composition ?? ""} />
            <button type="submit" disabled={busy}>Save metadata</button>
          </form>
        </details>
      </section>
    </div>
  );
}
