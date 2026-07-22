"use client";

/* eslint-disable @next/next/no-img-element */

import { useMemo, useState } from "react";

import { API_URL } from "@/lib/api";
import { localDate, localTime } from "@/lib/datetime";
import type { AssistedPublishingWorkspace as Workspace } from "@/lib/types";

export function AssistedPublishingWorkspace({
  workspace,
  onClose,
}: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const [index, setIndex] = useState(0);
  const [completed, setCompleted] = useState<Set<number>>(() => new Set());
  const [copyState, setCopyState] = useState("Copy caption");
  const item = workspace.items[index];
  const imageUrl = `${API_URL}${item.image_url}`;
  const date = localDate(item.planned_publish_at, workspace.timezone) ?? "Invalid date";
  const time = localTime(item.planned_publish_at, workspace.timezone) ?? "Invalid time";
  const completedCount = completed.size;
  const isCompleted = completed.has(item.proposal_id);
  const progressLabel = useMemo(
    () => `${completedCount} of ${workspace.items.length} marked done this session`,
    [completedCount, workspace.items.length],
  );

  async function copyCaption() {
    try {
      await navigator.clipboard.writeText(item.caption);
      setCopyState("Caption copied");
    } catch {
      setCopyState("Select and copy caption");
    }
  }

  function move(nextIndex: number) {
    setIndex(nextIndex);
    setCopyState("Copy caption");
  }

  function toggleCompleted() {
    setCompleted((current) => {
      const next = new Set(current);
      if (next.has(item.proposal_id)) next.delete(item.proposal_id);
      else next.add(item.proposal_id);
      return next;
    });
  }

  return (
    <section
      className="assisted-workspace"
      aria-labelledby="assisted-workspace-title"
    >
      <header>
        <div>
          <p className="eyebrow">Assisted publishing · {workspace.channel_name}</p>
          <h2 id="assisted-workspace-title">Native YouTube posting workspace</h2>
          <p>
            Runway prepared the exact approved payload. You remain responsible for the
            final Schedule action in YouTube.
          </p>
        </div>
        <button type="button" className="button secondary" onClick={onClose}>
          Close workspace
        </button>
      </header>

      <div className="assisted-progress" role="status" aria-live="polite">
        <strong>
          {index + 1} of {workspace.items.length}
        </strong>
        <span>{progressLabel}</span>
      </div>

      <div className="assisted-card">
        <figure>
          <img src={imageUrl} alt={`Approved image for post ${index + 1}`} />
          <figcaption>
            <a href={imageUrl} target="_blank" rel="noreferrer">
              Open exact image
            </a>
            <a href={imageUrl} download>
              Download exact image
            </a>
          </figcaption>
        </figure>

        <div className="assisted-instructions">
          <dl>
            <div>
              <dt>Date</dt>
              <dd>{date}</dd>
            </div>
            <div>
              <dt>Time</dt>
              <dd>{time}</dd>
            </div>
            <div>
              <dt>Timezone</dt>
              <dd>{workspace.timezone}</dd>
            </div>
          </dl>

          <label htmlFor={`assisted-caption-${item.proposal_id}`}>
            Exact caption
          </label>
          <textarea
            id={`assisted-caption-${item.proposal_id}`}
            value={item.caption}
            readOnly
            rows={6}
          />
          <div className="assisted-actions">
            <button type="button" className="button" onClick={copyCaption}>
              {copyState}
            </button>
            <a
              className="button secondary"
              href={workspace.youtube_url}
              target="_blank"
              rel="noreferrer"
              title="Opens the configured channel's native YouTube Posts page"
            >
              Open YouTube Studio ↗
            </a>
          </div>

          {item.warnings.length > 0 && (
            <ul className="assisted-warnings" aria-label="Publishing warnings">
              {item.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}

          <label className="assisted-complete">
            <input
              type="checkbox"
              checked={isCompleted}
              onChange={toggleCompleted}
            />
            Mark handled in this session
          </label>
          <small>
            This progress is session-local and is not external verification. Runway does
            not mark the post synchronized from this checkbox.
          </small>
        </div>
      </div>

      <nav className="assisted-navigation" aria-label="Prepared posts">
        <button
          type="button"
          className="button secondary"
          onClick={() => move(index - 1)}
          disabled={index === 0}
        >
          ← Previous
        </button>
        <button
          type="button"
          className="button secondary"
          onClick={() => move(index + 1)}
          disabled={index === workspace.items.length - 1}
        >
          Next →
        </button>
      </nav>
    </section>
  );
}
