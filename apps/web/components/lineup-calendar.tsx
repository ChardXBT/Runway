"use client";
/* eslint-disable @next/next/no-img-element */

import Link from "next/link";
import {
  KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { API_URL } from "@/lib/api";
import {
  actionError,
  ApiError,
  hasUncertainOutcome,
  readApiJson,
} from "@/lib/client-api";
import {
  localDate,
  scheduleIsPast,
  shiftIsoDate,
} from "@/lib/datetime";
import {
  conflictingLineupDates,
  isLineupSchedule,
  isProposal,
  isRecord,
} from "@/lib/guards";
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

function proposalSlot(proposal: Proposal) {
  return proposal.scheduled_publish_at ?? proposal.planned_publish_at;
}

function isoDate(date: Date) {
  return [
    date.getUTCFullYear(),
    String(date.getUTCMonth() + 1).padStart(2, "0"),
    String(date.getUTCDate()).padStart(2, "0"),
  ].join("-");
}

function monthStart(value: string | undefined, timeZone: string) {
  const source =
    localDate(value ?? new Date(), timeZone) ??
    localDate(new Date(), "UTC") ??
    "2000-01-01";
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
    approved: "Approved",
    internally_scheduled: "Waiting for YouTube",
    publishing: "Publishing now",
    externally_scheduled: "Scheduled on YouTube",
    publish_unverified: "Unverified · check required",
    publish_failed: "Failed · retry required",
    published: "Published",
  };
  return labels[status] ?? status.replaceAll("_", " ");
}

function statusTone(status: string) {
  if (status === "published" || status === "externally_scheduled") {
    return "success";
  }
  if (status === "publish_failed") return "danger";
  if (
    ["approved", "internally_scheduled", "publishing", "publish_unverified"].includes(
      status,
    )
  ) {
    return "warning";
  }
  return "neutral";
}

function isMutationResponse(
  value: unknown,
): value is { lineup: LineupSchedule } {
  return (
    isRecord(value) &&
    "lineup" in value &&
    isLineupSchedule(value.lineup)
  );
}

type RecoveryResponse = {
  lineup?: LineupSchedule;
  proposal?: Proposal;
};

function isRecoveryResponse(value: unknown): value is RecoveryResponse {
  if (!isRecord(value)) return false;
  const lineupValid =
    value.lineup === undefined || isLineupSchedule(value.lineup);
  const proposalValid =
    value.proposal === undefined || isProposal(value.proposal);
  return lineupValid && proposalValid && (value.lineup !== undefined || value.proposal !== undefined);
}

type DialogMode = "edit" | "remove" | null;
type LineupAction = "edit" | "remove" | "retry" | "verify" | null;

export function LineupCalendar({
  initialLineup,
  initialPublished = [],
  publishingEnabled,
}: {
  initialLineup: LineupSchedule;
  initialPublished?: Proposal[];
  publishingEnabled: boolean;
}) {
  const initialPublishedSorted = useMemo(
    () =>
      [...initialPublished]
        .filter((proposal) => proposal.status === "published")
        .sort(
          (first, second) =>
            new Date(proposalSlot(second)).getTime() -
            new Date(proposalSlot(first)).getTime(),
        ),
    [initialPublished],
  );
  const [lineup, setLineup] = useState(initialLineup);
  const initialSelection =
    initialLineup.scheduled[0] ?? initialPublishedSorted[0] ?? null;
  const initialMonthValue = initialSelection
    ? proposalSlot(initialSelection)
    : undefined;
  const [month, setMonth] = useState(() =>
    monthStart(initialMonthValue, initialLineup.timezone),
  );
  const [selectedId, setSelectedId] = useState<number | null>(
    initialSelection?.id ?? null,
  );
  const [dialog, setDialog] = useState<DialogMode>(null);
  const [draftCaption, setDraftCaption] = useState("");
  const [draftDate, setDraftDate] = useState("");
  const [busy, setBusy] = useState<LineupAction>(null);
  const [message, setMessage] = useState("");
  const [messageIsError, setMessageIsError] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [dialogOutcomeUncertain, setDialogOutcomeUncertain] = useState(false);
  const [uncertainProposalId, setUncertainProposalId] = useState<number | null>(
    null,
  );
  const actionLock = useRef(false);
  const dialogRef = useRef<HTMLElement>(null);
  const dialogOpener = useRef<HTMLElement | null>(null);
  const inspectorRef = useRef<HTMLElement>(null);
  const pageRef = useRef<HTMLElement>(null);

  const activeIds = useMemo(
    () => new Set(lineup.scheduled.map((proposal) => proposal.id)),
    [lineup.scheduled],
  );
  const published = useMemo(
    () => initialPublishedSorted.filter((proposal) => !activeIds.has(proposal.id)),
    [activeIds, initialPublishedSorted],
  );
  const allPosts = useMemo(
    () =>
      [...lineup.scheduled, ...published].sort(
        (first, second) =>
          new Date(proposalSlot(first)).getTime() -
          new Date(proposalSlot(second)).getTime(),
      ),
    [lineup.scheduled, published],
  );
  const selected =
    allPosts.find((proposal) => proposal.id === selectedId) ?? null;
  const integrityConflicts = useMemo(
    () => conflictingLineupDates(lineup),
    [lineup],
  );
  const lineupIntegritySafe = integrityConflicts.length === 0;

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

  const scheduledByDate = useMemo(() => {
    const map = new Map<string, Proposal[]>();
    for (const proposal of lineup.scheduled) {
      const key = localDate(proposalSlot(proposal), lineup.timezone);
      if (!key) continue;
      map.set(key, [...(map.get(key) ?? []), proposal]);
    }
    return map;
  }, [lineup.scheduled, lineup.timezone]);

  const byDate = useMemo(() => {
    const map = new Map<string, Proposal[]>();
    for (const proposal of allPosts) {
      const key = localDate(proposalSlot(proposal), lineup.timezone);
      if (!key) continue;
      map.set(key, [...(map.get(key) ?? []), proposal]);
    }
    return map;
  }, [allPosts, lineup.timezone]);

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

  const agendaPosts = useMemo(() => {
    const monthKey = month.toISOString().slice(0, 7);
    return allPosts.filter(
      (proposal) =>
        localDate(proposalSlot(proposal), lineup.timezone)?.slice(0, 7) ===
        monthKey,
    );
  }, [allPosts, lineup.timezone, month]);

  function setNotice(text: string, error = false) {
    setMessage(text);
    setMessageIsError(error);
  }

  function proposalIsFuture(proposal: Proposal) {
    const date = localDate(proposalSlot(proposal), lineup.timezone);
    return (
      date !== null &&
      scheduleIsPast(
        date,
        lineup.default_time,
        lineup.timezone,
      ) === false
    );
  }

  function canEditProposal(proposal: Proposal | null) {
    if (!proposal || !lineupIntegritySafe) return false;
    if (
      !["internally_scheduled", "publish_failed", "externally_scheduled"].includes(
        proposal.status,
      )
    ) {
      return false;
    }
    if (proposal.status === "externally_scheduled" && !publishingEnabled) {
      return false;
    }
    return proposalIsFuture(proposal);
  }

  function canRemoveProposal(proposal: Proposal | null) {
    if (!proposal || !lineupIntegritySafe) return false;
    if (["internally_scheduled", "publish_failed"].includes(proposal.status)) {
      return true;
    }
    return (
      proposal.status === "externally_scheduled" &&
      publishingEnabled &&
      proposalIsFuture(proposal)
    );
  }

  const selectedCanEdit = canEditProposal(selected);
  const selectedCanRemove = canRemoveProposal(selected);

  function immutableReason(proposal: Proposal) {
    if (!lineupIntegritySafe) {
      return "RunWay detected more than one active post on a date. Changes are locked until the Lineup is refreshed and verified.";
    }
    if (proposal.status === "published") {
      return "Published posts stay visible as locked history and cannot be edited, moved, or removed.";
    }
    if (proposal.status === "publishing") {
      return "This post is publishing now. Wait for a verified result before making changes.";
    }
    if (proposal.status === "publish_unverified") {
      return "This YouTube action is unverified. Verify it before making any other change.";
    }
    if (!proposalIsFuture(proposal)) {
      return "This release time has passed and can no longer be edited or moved safely.";
    }
    if (proposal.status === "externally_scheduled" && !publishingEnabled) {
      return "YouTube actions are off, so this existing YouTube release cannot be changed here.";
    }
    return "This post is not in a state that can be changed safely.";
  }

  function select(proposal: Proposal) {
    if (busy !== null) return;
    setSelectedId(proposal.id);
    setNotice("");
  }

  function rememberOpener(opener?: HTMLElement) {
    dialogOpener.current =
      opener ??
      (document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null);
  }

  function openEdit(dateOverride?: string, opener?: HTMLElement) {
    if (!selected || !canEditProposal(selected)) return;
    rememberOpener(opener);
    setDraftCaption(selected.final_caption);
    setDraftDate(
      dateOverride ??
        localDate(proposalSlot(selected), lineup.timezone) ??
        "",
    );
    setDialogError("");
    setDialogOutcomeUncertain(false);
    setDialog("edit");
    setNotice("");
  }

  function openRemove(opener?: HTMLElement) {
    if (!selected || !canRemoveProposal(selected)) return;
    rememberOpener(opener);
    setDialogError("");
    setDialogOutcomeUncertain(false);
    setDialog("remove");
    setNotice("");
  }

  function closeDialog() {
    if (busy !== null) return;
    setDialog(null);
    setDialogError("");
    setDialogOutcomeUncertain(false);
  }

  function quickTarget(offset: number) {
    if (!selected) return null;
    const current = localDate(proposalSlot(selected), lineup.timezone);
    if (!current) return null;
    return shiftIsoDate(current, offset);
  }

  function draftValidation(
    date = draftDate,
    caption = draftCaption,
  ): string | null {
    if (!selected) return "Select a post first.";
    if (!caption.trim()) return "Caption cannot be empty.";
    if (!date) return "Choose a release date.";
    const isPast = scheduleIsPast(
      date,
      lineup.default_time,
      lineup.timezone,
    );
    if (isPast === null) {
      return "The configured date, time, or timezone is invalid. Check Settings before moving this post.";
    }
    if (isPast) {
      return `Choose a future ${displayTime(lineup.default_time)} ${displayTimezone(lineup.timezone)} slot.`;
    }
    const occupants = (scheduledByDate.get(date) ?? []).filter(
      (proposal) => proposal.id !== selected.id,
    );
    if (occupants.length > 1) {
      return "That date already has conflicting RunWay posts. Refresh and verify Lineup before making changes.";
    }
    const occupant = occupants[0];
    if (occupant && !canEditProposal(occupant)) {
      return `${date} is occupied by “${occupant.final_caption},” which is ${statusLabel(occupant.status).toLowerCase()} and cannot be swapped safely.`;
    }
    return null;
  }

  function quickMoveDisabled(offset: number) {
    const target = quickTarget(offset);
    return (
      !selectedCanEdit ||
      !target ||
      draftValidation(target, selected?.final_caption ?? "") !== null
    );
  }

  async function parseMutation(response: Response) {
    const payload = await readApiJson(response, {
      validate: isMutationResponse,
      failureMessage: "Lineup could not complete that change.",
    });
    if (conflictingLineupDates(payload.lineup).length) {
      throw new ApiError(
        "RunWay received conflicting daily slots. Nothing has been confirmed in this view; refresh and verify Lineup.",
        { uncertainOutcome: true },
      );
    }
    return payload;
  }

  async function confirmEdit() {
    if (
      !selected ||
      actionLock.current ||
      dialogOutcomeUncertain ||
      !canEditProposal(selected)
    ) {
      return;
    }
    const validation = draftValidation();
    if (validation) {
      setDialogError(validation);
      return;
    }

    const originalDate =
      localDate(proposalSlot(selected), lineup.timezone) ?? "";
    const swapped = (scheduledByDate.get(draftDate) ?? []).find(
      (proposal) => proposal.id !== selected.id,
    );
    actionLock.current = true;
    setBusy("edit");
    setDialogError("");
    try {
      const response = await fetch(`${API_URL}/api/lineup/${selected.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          final_caption: draftCaption,
          new_date: draftDate === originalDate ? null : draftDate,
          confirmed: true,
        }),
      });
      const payload = await parseMutation(response);
      setLineup(payload.lineup);
      setDialog(null);
      setNotice(
        swapped
          ? `Confirmed. “${selected.final_caption}” and “${swapped.final_caption}” swapped release dates.`
          : draftDate === originalDate
            ? "Confirmed. The caption was updated and synchronization was verified."
            : "Confirmed. The post moved to the new release date.",
      );
    } catch (error) {
      const uncertain = hasUncertainOutcome(error);
      setDialogOutcomeUncertain(uncertain);
      const detail =
        error instanceof ApiError
          ? error.message
          : actionError(
              error,
              "The change failed. Your caption and date are preserved.",
              "The change response could not be verified. Your caption and date are preserved; refresh Lineup before trying again.",
            );
      setDialogError(detail);
      setNotice(detail, true);
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  async function confirmRemove() {
    if (
      !selected ||
      actionLock.current ||
      dialogOutcomeUncertain ||
      !canRemoveProposal(selected)
    ) {
      return;
    }
    actionLock.current = true;
    setBusy("remove");
    setDialogError("");
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
      setSelectedId(
        payload.lineup.scheduled[0]?.id ?? published[0]?.id ?? null,
      );
      setDialog(null);
      setNotice("Confirmed. The post was removed from Lineup.");
    } catch (error) {
      const uncertain = hasUncertainOutcome(error);
      setDialogOutcomeUncertain(uncertain);
      const detail =
        error instanceof ApiError
          ? error.message
          : actionError(
              error,
              "Remove failed. The post remains visible in this Lineup.",
              "The remove response could not be verified. Refresh Lineup and YouTube before trying again.",
            );
      setDialogError(detail);
      setNotice(detail, true);
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  async function retryOrVerify() {
    if (!selected || actionLock.current || uncertainProposalId === selected.id) {
      return;
    }
    const verifying = selected.status === "publish_unverified";
    if (
      !verifying &&
      !["internally_scheduled", "publish_failed"].includes(selected.status)
    ) {
      return;
    }
    actionLock.current = true;
    setBusy(verifying ? "verify" : "retry");
    setNotice(
      verifying
        ? "Checking YouTube without resubmitting…"
        : "Requesting one safe YouTube retry…",
    );
    try {
      const response = await fetch(
        verifying
          ? `${API_URL}/api/proposals/${selected.id}/youtube/verify`
          : `${API_URL}/api/lineup/${selected.id}/retry`,
        { method: "POST" },
      );
      const payload = await readApiJson(response, {
        validate: isRecoveryResponse,
        failureMessage: "YouTube recovery could not start.",
      });
      if (payload.lineup) {
        if (conflictingLineupDates(payload.lineup).length) {
          throw new ApiError(
            "The refreshed Lineup contains conflicting daily slots. Refresh and inspect Activity before taking another action.",
            { uncertainOutcome: true },
          );
        }
        setLineup(payload.lineup);
      } else if (payload.proposal) {
        setLineup((current) => ({
          ...current,
          scheduled: current.scheduled.map((item) =>
            item.id === payload.proposal?.id ? payload.proposal : item,
          ),
        }));
      }
      setNotice(
        verifying
          ? "YouTube verification finished. The displayed status is the verified result."
          : "The YouTube action was queued once.",
      );
    } catch (error) {
      const uncertain = hasUncertainOutcome(error);
      if (uncertain) setUncertainProposalId(selected.id);
      setNotice(
        error instanceof ApiError
          ? error.message
          : actionError(
              error,
              "YouTube recovery failed. No success has been recorded in this view.",
              "The recovery response could not be verified. Refresh Lineup before retrying or verifying again.",
            ),
        true,
      );
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  function moveMonth(offset: number) {
    setMonth((current) => {
      const next = new Date(current);
      next.setUTCMonth(next.getUTCMonth() + offset, 1);
      return next;
    });
  }

  function handleDialogKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.stopPropagation();
      closeDialog();
      return;
    }
    if (event.key !== "Tab" || !dialogRef.current) return;
    const focusable = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
      ),
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  useEffect(() => {
    if (!dialog) return;
    const previousOverflow = document.body.style.overflow;
    const opener = dialogOpener.current;
    const inspector = inspectorRef.current;
    const page = pageRef.current;
    document.body.style.overflow = "hidden";
    const frame = requestAnimationFrame(() => {
      const first = dialogRef.current?.querySelector<HTMLElement>(
        "[autofocus], button:not([disabled]), input:not([disabled]), textarea:not([disabled])",
      );
      first?.focus();
    });
    return () => {
      cancelAnimationFrame(frame);
      document.body.style.overflow = previousOverflow;
      requestAnimationFrame(() => {
        if (opener?.isConnected) {
          opener.focus();
        } else if (inspector?.isConnected) {
          inspector.focus();
        } else {
          page?.focus();
        }
      });
    };
  }, [dialog]);

  const occupiedTarget = draftDate
    ? scheduledByDate.get(draftDate)
    : undefined;
  const occupiedOther = occupiedTarget?.find(
    (proposal) => proposal.id !== selected?.id,
  );
  const currentDraftError = dialog === "edit" ? draftValidation() : null;
  const displayedDialogError = dialogError || currentDraftError || "";
  const today = localDate(new Date(), lineup.timezone) ?? undefined;

  return (
    <main ref={pageRef} className="lineup-page" tabIndex={-1}>
      <header className="lineup-header">
        <div>
          <p className="eyebrow">Scheduler / Qlob Lineup</p>
          <h1>Your release lineup.</h1>
          <p className="lede">
            {lineup.coverage} upcoming{" "}
            {lineup.coverage === 1 ? "post" : "posts"} organized. One RunWay post
            per day at {displayTime(lineup.default_time)}{" "}
            {displayTimezone(lineup.timezone)}. Published posts remain visible as
            locked history.
          </p>
        </div>
        <Link href="/review" className="button lineup-return">
          Back to Runway
        </Link>
      </header>

      <section className="lineup-toolbar" aria-label="Calendar controls">
        <div>
          <button
            type="button"
            onClick={() => moveMonth(-1)}
            aria-label="Previous month"
          >
            ←
          </button>
          <strong>{monthFormatter.format(month)}</strong>
          <button
            type="button"
            onClick={() => moveMonth(1)}
            aria-label="Next month"
          >
            →
          </button>
        </div>
        <span>
          Next opening ·{" "}
          {slotFormatter.format(new Date(lineup.next_available_at))}
        </span>
      </section>

      {!lineupIntegritySafe && (
        <section className="lineup-integrity-alert" role="alert">
          <strong>Daily-slot conflict detected.</strong>
          <span>
            {integrityConflicts.join(", ")} currently has more than one active
            RunWay post. Editing and removal are locked; refresh and verify Activity
            before continuing.
          </span>
        </section>
      )}

      {allPosts.length ? (
        <div className="lineup-layout">
          <section
            className="lineup-calendar"
            aria-label="RunWay release calendar"
          >
            <div className="lineup-section-label">
              <strong>Calendar</strong>
              <span>{monthFormatter.format(month)}</span>
            </div>
            <div className="lineup-weekdays" aria-hidden="true">
              {"Sun Mon Tue Wed Thu Fri Sat".split(" ").map((day) => (
                <span key={day}>{day}</span>
              ))}
            </div>
            <div className="lineup-calendar-grid">
              {calendarDays.map((day) => {
                const key = isoDate(day);
                const proposals = byDate.get(key) ?? [];
                const proposal =
                  proposals.find((item) => item.status !== "published") ??
                  proposals[0];
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
                        className={[
                          selectedId === proposal.id ? "selected" : "",
                          `status-tone-${statusTone(proposal.status)}`,
                        ]
                          .filter(Boolean)
                          .join(" ")}
                        onClick={() => select(proposal)}
                        aria-label={`Select ${proposal.final_caption}, ${dayFormatter.format(day)}, ${statusLabel(proposal.status)}`}
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

          <section className="lineup-agenda" aria-label="RunWay release agenda">
            <div className="lineup-section-label">
              <strong>{monthFormatter.format(month)}</strong>
              <span>{agendaPosts.length} posts</span>
            </div>
            {agendaPosts.length ? (
              agendaPosts.map((proposal) => (
                <button
                  type="button"
                  key={proposal.id}
                  className={[
                    selectedId === proposal.id ? "selected" : "",
                    `status-tone-${statusTone(proposal.status)}`,
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  onClick={() => select(proposal)}
                >
                  <time>{slotFormatter.format(new Date(proposalSlot(proposal)))}</time>
                  <span>{proposal.final_caption}</span>
                  <small>{statusLabel(proposal.status)}</small>
                </button>
              ))
            ) : (
              <p className="lineup-list-empty">No RunWay posts this month.</p>
            )}
          </section>

          <div className="lineup-side">
            <section className="lineup-upcoming" aria-label="Upcoming posts">
              <div className="lineup-section-label">
                <strong>Upcoming</strong>
                <span>{lineup.coverage} total</span>
              </div>
              <div className="lineup-upcoming-list">
                {lineup.scheduled.length ? (
                  lineup.scheduled.map((proposal) => (
                    <button
                      type="button"
                      key={proposal.id}
                      className={[
                        selectedId === proposal.id ? "selected" : "",
                        `status-tone-${statusTone(proposal.status)}`,
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      onClick={() => select(proposal)}
                    >
                      {proposal.candidate?.preview_url && (
                        <img
                          src={`${API_URL}${proposal.candidate.preview_url}`}
                          alt=""
                        />
                      )}
                      <span>
                        <time>
                          {slotFormatter.format(
                            new Date(proposalSlot(proposal)),
                          )}
                        </time>
                        <strong>{proposal.final_caption}</strong>
                        <small>{statusLabel(proposal.status)}</small>
                      </span>
                    </button>
                  ))
                ) : (
                  <p className="lineup-list-empty">No upcoming posts.</p>
                )}
              </div>
            </section>

            {published.length > 0 && (
              <section
                className="lineup-upcoming lineup-history"
                aria-label="Past published posts"
              >
                <div className="lineup-section-label">
                  <strong>Published history</strong>
                  <span>{published.length} visible</span>
                </div>
                <div className="lineup-upcoming-list">
                  {published.map((proposal) => (
                    <button
                      type="button"
                      key={proposal.id}
                      className={[
                        selectedId === proposal.id ? "selected" : "",
                        "status-tone-success",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      onClick={() => select(proposal)}
                    >
                      {proposal.candidate?.preview_url && (
                        <img
                          src={`${API_URL}${proposal.candidate.preview_url}`}
                          alt=""
                        />
                      )}
                      <span>
                        <time>
                          {slotFormatter.format(
                            new Date(proposalSlot(proposal)),
                          )}
                        </time>
                        <strong>{proposal.final_caption}</strong>
                        <small>Published</small>
                      </span>
                    </button>
                  ))}
                </div>
              </section>
            )}

            <aside
              ref={inspectorRef}
              className="lineup-inspector"
              aria-label="Selected post"
              tabIndex={-1}
            >
              {selected ? (
                <>
                  <div className="lineup-inspector-image">
                    {selected.candidate?.preview_url && (
                      <img
                        src={`${API_URL}${selected.candidate.preview_url}`}
                        alt="Selected Qlob post"
                      />
                    )}
                  </div>
                  <div className="lineup-inspector-copy">
                    <span
                      className={`status status-tone-${statusTone(selected.status)}`}
                    >
                      {statusLabel(selected.status)}
                    </span>
                    <time>
                      {slotFormatter.format(new Date(proposalSlot(selected)))}
                    </time>
                    <h2>{selected.final_caption}</h2>
                  </div>
                  <div className="lineup-inspector-actions">
                    <button
                      type="button"
                      className="button secondary"
                      onClick={(event) =>
                        openEdit(undefined, event.currentTarget)
                      }
                      disabled={
                        !selectedCanEdit ||
                        busy !== null ||
                        uncertainProposalId === selected.id
                      }
                    >
                      Edit or choose date
                    </button>
                    <div className="quick-move" aria-label="Quick move">
                      <button
                        type="button"
                        onClick={(event) => {
                          const target = quickTarget(-1);
                          if (target) openEdit(target, event.currentTarget);
                        }}
                        disabled={
                          busy !== null ||
                          uncertainProposalId === selected.id ||
                          quickMoveDisabled(-1)
                        }
                        title="Move one day earlier; an occupied date will swap"
                      >
                        ← One day
                      </button>
                      <button
                        type="button"
                        onClick={(event) => {
                          const target = quickTarget(1);
                          if (target) openEdit(target, event.currentTarget);
                        }}
                        disabled={
                          busy !== null ||
                          uncertainProposalId === selected.id ||
                          quickMoveDisabled(1)
                        }
                        title="Move one day later; an occupied date will swap"
                      >
                        One day →
                      </button>
                    </div>
                    {publishingEnabled &&
                      [
                        "internally_scheduled",
                        "publish_failed",
                        "publish_unverified",
                      ].includes(selected.status) && (
                        <button
                          type="button"
                          className="button sync"
                          onClick={retryOrVerify}
                          disabled={
                            busy !== null ||
                            uncertainProposalId === selected.id
                          }
                          aria-busy={
                            busy === "retry" || busy === "verify"
                          }
                        >
                          {busy === "retry"
                            ? "Retrying once…"
                            : busy === "verify"
                              ? "Verifying only…"
                              : selected.status === "publish_unverified"
                                ? "Verify YouTube"
                                : "Retry YouTube"}
                        </button>
                      )}
                    <button
                      type="button"
                      className="text-danger"
                      onClick={(event) => openRemove(event.currentTarget)}
                      disabled={
                        !selectedCanRemove ||
                        busy !== null ||
                        uncertainProposalId === selected.id
                      }
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
                    {selectedCanEdit || selectedCanRemove
                      ? publishingEnabled
                        ? "Every confirmed change is applied to YouTube first and shown as successful only after a valid response."
                        : "YouTube scheduling is off; confirmed changes affect the local Lineup only."
                      : immutableReason(selected)}
                  </p>
                </>
              ) : null}
            </aside>
          </div>
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

      <p
        className={messageIsError ? "lineup-message error" : "lineup-message"}
        role={messageIsError ? "alert" : "status"}
        aria-live={messageIsError ? "assertive" : "polite"}
      >
        {message}
      </p>

      {dialog && selected && (
        <div
          className="dialog-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeDialog();
          }}
          onKeyDown={handleDialogKeyDown}
        >
          <section
            ref={dialogRef}
            className="lineup-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="lineup-dialog-title"
            aria-describedby={
              dialog === "edit"
                ? "lineup-dialog-help lineup-dialog-error"
                : "lineup-remove-copy lineup-dialog-error"
            }
            onMouseDown={(event) => event.stopPropagation()}
          >
            {dialog === "edit" ? (
              <>
                <p className="eyebrow">Confirm a Lineup change</p>
                <h2 id="lineup-dialog-title">Refine the release.</h2>
                <p id="lineup-dialog-help" className="dialog-intro">
                  Change the caption, choose a date, or use the one-day controls.
                  Moving onto an occupied date swaps the two posts after confirmation.
                </p>
                <label htmlFor="lineup-caption">
                  <span>Caption</span>
                  <textarea
                    id="lineup-caption"
                    aria-label="Caption"
                    value={draftCaption}
                    onChange={(event) => {
                      setDraftCaption(event.target.value);
                      setDialogError("");
                    }}
                    maxLength={1000}
                    rows={5}
                    autoFocus
                    disabled={busy !== null || dialogOutcomeUncertain}
                  />
                  <small>
                    {draftCaption.length} / 1000 · punctuation is preserved
                  </small>
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
                    min={today}
                    onChange={(event) => {
                      setDraftDate(event.target.value);
                      setDialogError("");
                    }}
                    aria-invalid={currentDraftError ? "true" : undefined}
                    disabled={busy !== null || dialogOutcomeUncertain}
                  />
                </label>
                {occupiedOther && !currentDraftError && (
                  <p className="swap-notice">
                    <strong>Swap on confirmation.</strong> {draftDate} currently
                    holds “{occupiedOther.final_caption}.” Confirming moves that post
                    to this post’s current date—there will still be only one RunWay
                    post per day.
                  </p>
                )}
                {dialogOutcomeUncertain && (
                  <a className="dialog-recovery" href="/lineup">
                    Refresh Lineup to verify
                  </a>
                )}
                <p
                  id="lineup-dialog-error"
                  className="dialog-error"
                  role={displayedDialogError ? "alert" : undefined}
                >
                  {displayedDialogError}
                </p>
                <div className="dialog-actions">
                  <button
                    type="button"
                    className="button secondary"
                    onClick={closeDialog}
                    disabled={busy !== null}
                  >
                    Keep current
                  </button>
                  <button
                    type="button"
                    className="button approve"
                    onClick={confirmEdit}
                    disabled={
                      busy !== null ||
                      dialogOutcomeUncertain ||
                      currentDraftError !== null
                    }
                    aria-busy={busy === "edit"}
                  >
                    {busy === "edit" ? "Verifying change…" : "Confirm changes"}
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="eyebrow danger">Remove from Lineup</p>
                <h2 id="lineup-dialog-title">Pull this release?</h2>
                <p id="lineup-remove-copy" className="dialog-copy">
                  This explicitly removes the scheduled post from YouTube and cancels
                  its RunWay slot. The decision remains in Activity.
                </p>
                {dialogOutcomeUncertain && (
                  <a className="dialog-recovery" href="/lineup">
                    Refresh Lineup to verify
                  </a>
                )}
                <p
                  id="lineup-dialog-error"
                  className="dialog-error"
                  role={dialogError ? "alert" : undefined}
                >
                  {dialogError}
                </p>
                <div className="dialog-actions">
                  <button
                    type="button"
                    className="button secondary"
                    onClick={closeDialog}
                    disabled={busy !== null}
                    autoFocus
                  >
                    Keep it
                  </button>
                  <button
                    type="button"
                    className="button reject"
                    onClick={confirmRemove}
                    disabled={busy !== null || dialogOutcomeUncertain}
                    aria-busy={busy === "remove"}
                  >
                    {busy === "remove" ? "Verifying removal…" : "Confirm remove"}
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
