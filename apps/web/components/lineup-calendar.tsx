"use client";
/* eslint-disable @next/next/no-img-element */

import Link from "next/link";
import { useMemo, useState } from "react";

import { API_URL } from "@/lib/api";
import type { LineupSchedule, Proposal } from "@/lib/types";

const monthFormatter = new Intl.DateTimeFormat("en-US", {
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

const dayFormatter = new Intl.DateTimeFormat("en-US", {
  weekday: "short",
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

function localDate(value: string, timeZone: string) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone,
  }).formatToParts(new Date(value));
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function isoDate(date: Date) {
  return [
    date.getUTCFullYear(),
    String(date.getUTCMonth() + 1).padStart(2, "0"),
    String(date.getUTCDate()).padStart(2, "0"),
  ].join("-");
}

function monthStart(value: string | undefined, timeZone: string) {
  const source = localDate(value ?? new Date().toISOString(), timeZone);
  return new Date(`${source.slice(0, 7)}-01T00:00:00Z`);
}

function displayTime(value: string) {
  const [hour, minute] = value.split(":").map(Number);
  return new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: "UTC",
  }).format(new Date(Date.UTC(2000, 0, 1, hour, minute)));
}

function displayTimezone(value: string) {
  return value === "America/Toronto" ? "Eastern" : value.replaceAll("_", " ");
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    internally_scheduled: "Waiting for YouTube",
    publishing: "Sending to YouTube",
    externally_scheduled: "On YouTube",
    publish_unverified: "Needs verification",
    publish_failed: "Retry needed",
    published: "Published",
  };
  return labels[status] ?? status.replaceAll("_", " ");
}

type DialogMode = "edit" | "remove" | null;

export function LineupCalendar({
  initialLineup,
  publishingEnabled,
}: {
  initialLineup: LineupSchedule;
  publishingEnabled: boolean;
}) {
  const [lineup, setLineup] = useState(initialLineup);
  const [month, setMonth] = useState(() =>
    monthStart(
      initialLineup.scheduled[0]?.scheduled_publish_at ?? undefined,
      initialLineup.timezone,
    ),
  );
  const [selectedId, setSelectedId] = useState<number | null>(
    initialLineup.scheduled[0]?.id ?? null,
  );
  const [dialog, setDialog] = useState<DialogMode>(null);
  const [draftCaption, setDraftCaption] = useState("");
  const [draftDate, setDraftDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const selected =
    lineup.scheduled.find((proposal) => proposal.id === selectedId) ?? null;
  const selectedMutable =
    selected !== null &&
    ["internally_scheduled", "publish_failed", "externally_scheduled"].includes(
      selected.status,
    ) &&
    (selected.status !== "externally_scheduled" || publishingEnabled);
  const slotFormatter = useMemo(
    () =>
      new Intl.DateTimeFormat("en-US", {
        weekday: "short",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
        timeZone: lineup.timezone,
      }),
    [lineup.timezone],
  );
  const byDate = useMemo(() => {
    const map = new Map<string, Proposal[]>();
    for (const proposal of lineup.scheduled) {
      const slot = proposal.scheduled_publish_at ?? proposal.planned_publish_at;
      const key = localDate(slot, lineup.timezone);
      map.set(key, [...(map.get(key) ?? []), proposal]);
    }
    return map;
  }, [lineup.scheduled, lineup.timezone]);

  const calendarDays = useMemo(() => {
    const first = new Date(month);
    const gridStart = new Date(first);
    gridStart.setUTCDate(1 - first.getUTCDay());
    return Array.from({ length: 42 }, (_, index) => {
      const day = new Date(gridStart);
      day.setUTCDate(gridStart.getUTCDate() + index);
      return day;
    });
  }, [month]);

  function select(proposal: Proposal) {
    setSelectedId(proposal.id);
    setMessage("");
  }

  function openEdit() {
    if (!selected) return;
    setDraftCaption(selected.final_caption);
    setDraftDate(
      localDate(
        selected.scheduled_publish_at ?? selected.planned_publish_at,
        lineup.timezone,
      ),
    );
    setDialog("edit");
    setMessage("");
  }

  function closeDialog() {
    if (busy) return;
    setDialog(null);
  }

  async function parseMutation(response: Response) {
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Lineup could not complete that change.");
    }
    return payload as { lineup: LineupSchedule };
  }

  async function confirmEdit() {
    if (!selected || busy) return;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`${API_URL}/api/lineup/${selected.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          final_caption: draftCaption,
          new_date:
            draftDate ===
            localDate(
              selected.scheduled_publish_at ?? selected.planned_publish_at,
              lineup.timezone,
            )
              ? null
              : draftDate,
          confirmed: true,
        }),
      });
      const payload = await parseMutation(response);
      setLineup(payload.lineup);
      setDialog(null);
      const swapped = byDate
        .get(draftDate)
        ?.find((proposal) => proposal.id !== selected.id);
      setMessage(
        swapped
          ? "Confirmed. The two release dates were swapped."
          : "Confirmed. Lineup and YouTube are in sync.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The change failed.");
    } finally {
      setBusy(false);
    }
  }

  async function confirmRemove() {
    if (!selected || busy) return;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(
        `${API_URL}/api/lineup/${selected.id}/remove`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirmed: true }),
        },
      );
      const payload = await parseMutation(response);
      setLineup(payload.lineup);
      setSelectedId(payload.lineup.scheduled[0]?.id ?? null);
      setDialog(null);
      setMessage("Confirmed. The post was removed from Lineup.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Remove failed.");
    } finally {
      setBusy(false);
    }
  }

  function moveMonth(offset: number) {
    setMonth((current) => {
      const next = new Date(current);
      next.setUTCMonth(next.getUTCMonth() + offset, 1);
      return next;
    });
  }

  const occupiedTarget = draftDate ? byDate.get(draftDate) : undefined;
  const occupiedOther = occupiedTarget?.find(
    (proposal) => proposal.id !== selected?.id,
  );

  return (
    <main className="lineup-page">
      <header className="lineup-header">
        <div>
          <p className="eyebrow">Lineup / Qlob releases</p>
          <h1>{lineup.coverage} ready to go.</h1>
          <p className="lede">
            One RunWay post per day at {displayTime(lineup.default_time)}{" "}
            {displayTimezone(lineup.timezone)}. Select any look to refine, move, swap,
            or remove it.
          </p>
        </div>
        <Link href="/review" className="button lineup-return">
          Back to Runway
        </Link>
      </header>

      <section className="lineup-toolbar" aria-label="Calendar controls">
        <div>
          <button type="button" onClick={() => moveMonth(-1)} aria-label="Previous month">
            ←
          </button>
          <strong>{monthFormatter.format(month)}</strong>
          <button type="button" onClick={() => moveMonth(1)} aria-label="Next month">
            →
          </button>
        </div>
        <span>
          Next opening · {slotFormatter.format(new Date(lineup.next_available_at))}
        </span>
      </section>

      {lineup.scheduled.length ? (
        <div className="lineup-layout">
          <section className="lineup-calendar" aria-label="RunWay release calendar">
            <div className="lineup-weekdays" aria-hidden="true">
              {"Sun Mon Tue Wed Thu Fri Sat".split(" ").map((day) => (
                <span key={day}>{day}</span>
              ))}
            </div>
            <div className="lineup-calendar-grid">
              {calendarDays.map((day) => {
                const key = isoDate(day);
                const proposals = byDate.get(key) ?? [];
                const proposal = proposals[0];
                const inMonth = day.getUTCMonth() === month.getUTCMonth();
                return (
                  <article
                    className={[
                      "lineup-day",
                      inMonth ? "" : "outside",
                      proposal ? "occupied" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    key={key}
                  >
                    <time dateTime={key}>{day.getUTCDate()}</time>
                    {proposal && (
                      <button
                        type="button"
                        className={selectedId === proposal.id ? "selected" : ""}
                        onClick={() => select(proposal)}
                        aria-label={`Select ${proposal.final_caption}, ${dayFormatter.format(day)}`}
                      >
                        {proposal.candidate?.preview_url && (
                          <img
                            src={`${API_URL}${proposal.candidate.preview_url}`}
                            alt=""
                          />
                        )}
                        <span>{proposal.final_caption}</span>
                        <small>{statusLabel(proposal.status)}</small>
                      </button>
                    )}
                    {proposals.length > 1 && (
                      <span className="lineup-conflict">
                        +{proposals.length - 1} conflict
                      </span>
                    )}
                  </article>
                );
              })}
            </div>
          </section>

          <section className="lineup-agenda" aria-label="Scheduled posts agenda">
            {lineup.scheduled.map((proposal) => {
              const slot = proposal.scheduled_publish_at ?? proposal.planned_publish_at;
              return (
                <button
                  type="button"
                  key={proposal.id}
                  className={selectedId === proposal.id ? "selected" : ""}
                  onClick={() => select(proposal)}
                >
                  <time>{slotFormatter.format(new Date(slot))}</time>
                  <span>{proposal.final_caption}</span>
                  <small>{statusLabel(proposal.status)}</small>
                </button>
              );
            })}
          </section>

          <aside className="lineup-inspector" aria-label="Selected post">
            {selected ? (
              <>
                <div className="lineup-inspector-image">
                  {selected.candidate?.preview_url && (
                    <img
                      src={`${API_URL}${selected.candidate.preview_url}`}
                      alt="Selected scheduled Qlob post"
                    />
                  )}
                </div>
                <div className="lineup-inspector-copy">
                  <span className={`status status-${selected.status}`}>
                    {statusLabel(selected.status)}
                  </span>
                  <time>
                    {slotFormatter.format(
                      new Date(
                        selected.scheduled_publish_at ?? selected.planned_publish_at,
                      ),
                    )}
                  </time>
                  <h2>{selected.final_caption}</h2>
                </div>
                <div className="lineup-inspector-actions">
                  <button
                    type="button"
                    className="button secondary"
                    onClick={openEdit}
                    disabled={!selectedMutable}
                  >
                    Modify
                  </button>
                  <button
                    type="button"
                    className="text-danger"
                    onClick={() => setDialog("remove")}
                    disabled={!selectedMutable}
                  >
                    Remove
                  </button>
                  {selected.external_post_url && (
                    <a
                      href={selected.external_post_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      View on YouTube ↗
                    </a>
                  )}
                </div>
                <p className="lineup-sync-note">
                  {publishingEnabled
                    ? selectedMutable
                      ? "Confirmed changes are applied to YouTube first and saved here only after verification."
                      : "This release is in flight or no longer safely editable. Activity keeps the full record."
                    : selected.status === "externally_scheduled"
                      ? "YouTube scheduling is off, so an existing YouTube release cannot be changed here."
                      : "YouTube scheduling is off; changes affect the local Lineup only."}
                </p>
              </>
            ) : null}
          </aside>
        </div>
      ) : (
        <section className="lineup-empty">
          <h2>Nothing lined up yet.</h2>
          <p>Accept a look on Runway and it will take the next open daily slot.</p>
          <Link href="/review" className="button">
            Open Runway
          </Link>
        </section>
      )}

      <p className="lineup-message" role="status">
        {message}
      </p>

      {dialog && selected && (
        <div
          className="dialog-backdrop"
          onMouseDown={closeDialog}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation();
              closeDialog();
            }
          }}
        >
          <section
            className="lineup-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="lineup-dialog-title"
            onMouseDown={(event) => event.stopPropagation()}
          >
            {dialog === "edit" ? (
              <>
                <p className="eyebrow">Confirm a Lineup change</p>
                <h2 id="lineup-dialog-title">Refine the release.</h2>
                <label htmlFor="lineup-caption">
                  <span>Caption</span>
                  <textarea
                    id="lineup-caption"
                    aria-label="Caption"
                    value={draftCaption}
                    onChange={(event) => setDraftCaption(event.target.value)}
                    maxLength={1000}
                    rows={5}
                    autoFocus
                  />
                  <small>{draftCaption.length} / 1000 · punctuation is preserved</small>
                </label>
                <label htmlFor="lineup-date">
                  <span>
                    Release date · {displayTime(lineup.default_time)}{" "}
                    {displayTimezone(lineup.timezone)}
                  </span>
                  <input
                    id="lineup-date"
                    aria-label="Release date"
                    type="date"
                    value={draftDate}
                    onChange={(event) => setDraftDate(event.target.value)}
                  />
                </label>
                {occupiedOther && (
                  <p className="swap-notice">
                    {draftDate} is occupied. Confirming swaps the two release dates.
                  </p>
                )}
                <div className="dialog-actions">
                  <button
                    type="button"
                    className="button secondary"
                    onClick={closeDialog}
                    disabled={busy}
                  >
                    Keep current
                  </button>
                  <button
                    type="button"
                    className="button approve"
                    onClick={confirmEdit}
                    disabled={busy || !draftCaption.trim() || !draftDate}
                  >
                    {busy ? "Verifying…" : "Confirm changes"}
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="eyebrow danger">Remove from Lineup</p>
                <h2 id="lineup-dialog-title">Pull this release?</h2>
                <p className="dialog-copy">
                  This removes the scheduled post from YouTube and cancels its RunWay slot.
                  The decision remains in Activity.
                </p>
                <div className="dialog-actions">
                  <button
                    type="button"
                    className="button secondary"
                    onClick={closeDialog}
                    disabled={busy}
                  >
                    Keep it
                  </button>
                  <button
                    type="button"
                    className="button reject"
                    onClick={confirmRemove}
                    disabled={busy}
                  >
                    {busy ? "Verifying…" : "Confirm remove"}
                  </button>
                </div>
              </>
            )}
          </section>
        </div>
      )}
    </main>
  );
}
