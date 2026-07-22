"use client";
/* eslint-disable @next/next/no-img-element */

import Link from "next/link";
import {
  DragEvent,
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
  localTime,
  scheduleIsPast,
  shiftIsoDate,
  zonedScheduleIso,
} from "@/lib/datetime";
import {
  conflictingLineupDates,
  isLineupSchedule,
  isLineupPushResponse,
  isPublisherQueueStatus,
  isProposal,
  isRecord,
} from "@/lib/guards";
import type {
  AssistedPublishingWorkspace as AssistedWorkspace,
  LineupSchedule,
  Proposal,
  PublishingMode,
  PublisherQueueStatus,
} from "@/lib/types";

import { AssistedPublishingWorkspace } from "./assisted-publishing-workspace";

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
    internally_scheduled: "Waiting in Lineup",
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

type DialogMode = "edit" | "remove" | "publish" | null;
type LineupAction = "edit" | "remove" | "retry" | "verify" | "push" | null;

const idlePublisherQueue: PublisherQueueStatus = {
  running: false,
  queued: 0,
  paused: false,
  paused_reason: null,
};

export function LineupCalendar({
  initialLineup,
  initialPublished = [],
  publishingEnabled,
  publishingMode = "assisted",
  channelName = "Qlob",
  initialPublisherQueue = idlePublisherQueue,
}: {
  initialLineup: LineupSchedule;
  initialPublished?: Proposal[];
  publishingEnabled: boolean;
  publishingMode?: PublishingMode;
  channelName?: string;
  initialPublisherQueue?: PublisherQueueStatus;
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
  const [publisherQueue, setPublisherQueue] = useState(initialPublisherQueue);
  const [watchPublisher, setWatchPublisher] = useState(
    initialPublisherQueue.running || initialPublisherQueue.queued > 0,
  );
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
  const [draftTime, setDraftTime] = useState("");
  const [busy, setBusy] = useState<LineupAction>(null);
  const [message, setMessage] = useState("");
  const [messageIsError, setMessageIsError] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [dialogOutcomeUncertain, setDialogOutcomeUncertain] = useState(false);
  const [uncertainProposalId, setUncertainProposalId] = useState<number | null>(
    null,
  );
  const [pushOutcomeUncertain, setPushOutcomeUncertain] = useState(false);
  const [draggedId, setDraggedId] = useState<number | null>(null);
  const [dragTargetDate, setDragTargetDate] = useState<string | null>(null);
  const [recentlyChangedIds, setRecentlyChangedIds] = useState<number[]>([]);
  const [assistedWorkspace, setAssistedWorkspace] =
    useState<AssistedWorkspace | null>(null);
  const [renderedAt] = useState(() => Date.now());
  const actionLock = useRef(false);
  const dialogRef = useRef<HTMLElement>(null);
  const dialogOpener = useRef<HTMLElement | null>(null);
  const inspectorRef = useRef<HTMLElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);

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
    const daysInMonth = new Date(
      Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0),
    ).getUTCDate();
    const visibleDays = Math.ceil((first.getUTCDay() + daysInMonth) / 7) * 7;
    return Array.from({ length: visibleDays }, (_, index) => {
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
  const waitingForYouTube = useMemo(
    () =>
      lineup.scheduled.filter(
        (proposal) =>
          proposal.status === "internally_scheduled" ||
          proposal.status === "publish_failed",
      ),
    [lineup.scheduled],
  );

  function setNotice(text: string, error = false) {
    setMessage(text);
    setMessageIsError(error);
  }

  function proposalIsFuture(proposal: Proposal) {
    const instant = new Date(proposalSlot(proposal)).getTime();
    return Number.isFinite(instant) && instant > renderedAt + 5 * 60_000;
  }

  function canEditProposal(proposal: Proposal | null) {
    if (!proposal || !lineupIntegritySafe) return false;
    if (
      !["internally_scheduled", "publish_failed"].includes(
        proposal.status,
      )
    ) {
      return false;
    }
    return proposalIsFuture(proposal);
  }

  function canRemoveProposal(proposal: Proposal | null) {
    if (!proposal || !lineupIntegritySafe) return false;
    if (["internally_scheduled", "publish_failed"].includes(proposal.status)) {
      return true;
    }
    return false;
  }

  const selectedCanEdit = canEditProposal(selected);
  const selectedCanRemove = canRemoveProposal(selected);

  function immutableReason(proposal: Proposal) {
    if (!lineupIntegritySafe) {
      return "Runway detected more than one active post on a date. Changes are locked until the Lineup is refreshed and verified.";
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
    if (proposal.status === "externally_scheduled") {
      return "This post is already confirmed on YouTube and is treated as immutable. Normal Lineup edits and removal never change external content.";
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

  function openEditFor(
    proposal: Proposal,
    dateOverride?: string,
    opener?: HTMLElement,
  ) {
    if (!canEditProposal(proposal)) return;
    rememberOpener(opener);
    setSelectedId(proposal.id);
    setDraftCaption(proposal.final_caption);
    setDraftDate(
      dateOverride ??
        localDate(proposalSlot(proposal), lineup.timezone) ??
        "",
    );
    setDraftTime(
      localTime(proposalSlot(proposal), lineup.timezone) ?? lineup.default_time,
    );
    setDialogError("");
    setDialogOutcomeUncertain(false);
    setDialog("edit");
    setNotice("");
  }

  function openEdit(dateOverride?: string, opener?: HTMLElement) {
    if (!selected) return;
    openEditFor(selected, dateOverride, opener);
  }

  function openRemove(opener?: HTMLElement) {
    if (!selected || !canRemoveProposal(selected)) return;
    rememberOpener(opener);
    setDialogError("");
    setDialogOutcomeUncertain(false);
    setDialog("remove");
    setNotice("");
  }

  function openPublish(opener?: HTMLElement) {
    if (waitingForYouTube.length === 0 || busy !== null) return;
    rememberOpener(opener);
    setDialogError("");
    setDialogOutcomeUncertain(false);
    setDialog("publish");
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

  function draftValidationFor(
    proposal: Proposal | null,
    date: string,
    caption: string,
    time = proposal
      ? localTime(proposalSlot(proposal), lineup.timezone) ?? lineup.default_time
      : lineup.default_time,
  ): string | null {
    if (!proposal) return "Select a post first.";
    if (!caption.trim()) return "Caption cannot be empty.";
    if (!date) return "Choose a release date.";
    if (!time) return "Choose a release time.";
    const isPast = scheduleIsPast(
      date,
      time,
      lineup.timezone,
    );
    if (isPast === null) {
      return "The configured date, time, or timezone is invalid. Check Settings before moving this post.";
    }
    if (isPast) {
      return `Choose a future ${displayTime(time)} ${displayTimezone(lineup.timezone)} slot.`;
    }
    const occupants = (scheduledByDate.get(date) ?? []).filter(
      (occupant) => occupant.id !== proposal.id,
    );
    if (occupants.length > 1) {
      return "That date already has conflicting Runway posts. Refresh and verify Lineup before making changes.";
    }
    const occupant = occupants[0];
    if (occupant && !canEditProposal(occupant)) {
      return `${date} is occupied by “${occupant.final_caption},” which is ${statusLabel(occupant.status).toLowerCase()} and cannot be swapped safely.`;
    }
    return null;
  }

  function draftValidation(
    date = draftDate,
    caption = draftCaption,
    time = draftTime,
  ): string | null {
    return draftValidationFor(selected, date, caption, time);
  }

  function quickMoveDisabled(offset: number) {
    const target = quickTarget(offset);
    return (
      !selectedCanEdit ||
      !target ||
      draftValidation(
        target,
        selected?.final_caption ?? "",
        selected
          ? localTime(proposalSlot(selected), lineup.timezone) ?? lineup.default_time
          : lineup.default_time,
      ) !== null
    );
  }

  async function parseMutation(response: Response) {
    const payload = await readApiJson(response, {
      validate: isMutationResponse,
      failureMessage: "Lineup could not complete that change.",
    });
    if (conflictingLineupDates(payload.lineup).length) {
      throw new ApiError(
        "Runway received conflicting daily slots. Nothing has been confirmed in this view; refresh and verify Lineup.",
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
    const originalTime =
      localTime(proposalSlot(selected), lineup.timezone) ?? lineup.default_time;
    const timestampChanged =
      draftDate !== originalDate || draftTime !== originalTime;
    const newTimestamp = timestampChanged
      ? zonedScheduleIso(draftDate, draftTime, lineup.timezone)
      : null;
    if (timestampChanged && !newTimestamp) {
      setDialogError(
        "That local date and time does not exist in the configured timezone.",
      );
      return;
    }
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
          scheduled_publish_at: newTimestamp,
          confirmed: true,
        }),
      });
      const payload = await parseMutation(response);
      setLineup(payload.lineup);
      setRecentlyChangedIds(
        swapped ? [selected.id, swapped.id] : [selected.id],
      );
      setDialog(null);
      setNotice(
        swapped
          ? `Confirmed. “${selected.final_caption}” and “${swapped.final_caption}” swapped release dates.`
          : !timestampChanged
            ? "Confirmed. The caption was updated locally and is ready for the next explicit publishing action."
            : "Confirmed. The post moved to the new local release time.",
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

  function beginDrag(
    event: DragEvent<HTMLButtonElement>,
    proposal: Proposal,
  ) {
    if (busy !== null || !canEditProposal(proposal)) {
      event.preventDefault();
      return;
    }
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(proposal.id));
    setSelectedId(proposal.id);
    setDraggedId(proposal.id);
    setDragTargetDate(null);
    setNotice(`Moving “${proposal.final_caption}” — choose a future date.`);
  }

  function dragOverDate(event: DragEvent<HTMLElement>, date: string) {
    const proposal = lineup.scheduled.find((item) => item.id === draggedId);
    if (
      !proposal ||
      draftValidationFor(proposal, date, proposal.final_caption) !== null
    ) {
      event.dataTransfer.dropEffect = "none";
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    setDragTargetDate(date);
  }

  function dropOnDate(
    event: DragEvent<HTMLElement>,
    date: string,
  ) {
    event.preventDefault();
    const proposal = lineup.scheduled.find((item) => item.id === draggedId);
    setDraggedId(null);
    setDragTargetDate(null);
    if (!proposal) return;
    const validation = draftValidationFor(
      proposal,
      date,
      proposal.final_caption,
    );
    if (validation) {
      setNotice(validation, true);
      return;
    }
    const currentDate = localDate(proposalSlot(proposal), lineup.timezone);
    if (date === currentDate) {
      setNotice("That post is already on this date.");
      return;
    }
    openEditFor(proposal, date, event.currentTarget);
  }

  function endDrag() {
    setDraggedId(null);
    setDragTargetDate(null);
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

  async function pushLineupToYouTube() {
    if (
      actionLock.current ||
      busy !== null ||
      pushOutcomeUncertain ||
      (publishingMode === "authorized_browser" && !publishingEnabled) ||
      waitingForYouTube.length === 0 ||
      (publishingMode === "authorized_browser" && publisherQueue.paused)
    ) {
      return;
    }
    actionLock.current = true;
    setBusy("push");
    setNotice(
      publishingMode === "assisted"
        ? `Validating ${waitingForYouTube.length} ${waitingForYouTube.length === 1 ? "post" : "posts"} for assisted preparation…`
        : `Queuing ${waitingForYouTube.length} ${waitingForYouTube.length === 1 ? "post" : "posts"} for authorized browser handling…`,
    );
    try {
      const response = await fetch(`${API_URL}/api/lineup/push`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirmed: true,
          mode: publishingMode,
          proposal_ids: waitingForYouTube.map((proposal) => proposal.id),
        }),
      });
      const payload = await readApiJson(response, {
        validate: isLineupPushResponse,
        failureMessage: "The Lineup could not be pushed to YouTube.",
        malformedMessage:
          "Runway could not verify the YouTube queue response. Refresh Lineup before trying again.",
      });
      if (conflictingLineupDates(payload.lineup).length) {
        throw new ApiError(
          "The refreshed Lineup contains conflicting daily slots. Refresh and inspect Activity before taking another action.",
          { uncertainOutcome: true },
        );
      }
      setLineup(payload.lineup);
      setPublisherQueue(payload.publisher_queue);
      setDialog(null);
      if (payload.mode === "assisted" && payload.assisted_workspace) {
        setAssistedWorkspace(payload.assisted_workspace);
        setWatchPublisher(false);
        setNotice(
          `${payload.detail} Nothing was queued and no browser was opened by Runway.`,
        );
      } else {
        setWatchPublisher(payload.queued_proposal_ids.length > 0);
        setNotice(
          payload.queued_proposal_ids.length > 0
            ? `${payload.detail} Runway will update each post as it is individually verified.`
            : payload.detail,
        );
      }
    } catch (error) {
      const uncertain = hasUncertainOutcome(error);
      setPushOutcomeUncertain(uncertain);
      const detail =
        error instanceof ApiError
          ? error.message
          : actionError(
              error,
              publishingMode === "assisted"
                ? "The assisted workspace was not prepared. Review the validation error and try again."
                : "The Lineup was not queued. Check the publisher connection and try again.",
              "The queue response could not be verified. Refresh Lineup before pushing again.",
            );
      setDialogError(detail);
      setNotice(detail, true);
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

  useEffect(() => {
    if (recentlyChangedIds.length === 0) return;
    const timer = window.setTimeout(() => setRecentlyChangedIds([]), 700);
    return () => window.clearTimeout(timer);
  }, [recentlyChangedIds]);

  useEffect(() => {
    if (
      !watchPublisher ||
      publishingMode !== "authorized_browser" ||
      !publishingEnabled
    )
      return;
    let cancelled = false;
    let timer: number | undefined;

    async function refreshPublisher() {
      try {
        const [lineupResponse, queueResponse] = await Promise.all([
          fetch(`${API_URL}/api/queue?limit=5000`, { cache: "no-store" }),
          fetch(`${API_URL}/api/publisher/queue`, { cache: "no-store" }),
        ]);
        const [freshLineup, freshQueue] = await Promise.all([
          readApiJson(lineupResponse, {
            validate: isLineupSchedule,
            failureMessage: "Lineup status could not be refreshed.",
          }),
          readApiJson(queueResponse, {
            validate: isPublisherQueueStatus,
            failureMessage: "YouTube queue status could not be refreshed.",
          }),
        ]);
        if (cancelled) return;
        if (conflictingLineupDates(freshLineup).length) {
          throw new ApiError(
            "The refreshed Lineup contains conflicting daily slots. Refresh and inspect Activity before taking another action.",
            { uncertainOutcome: true },
          );
        }
        setLineup(freshLineup);
        setPublisherQueue(freshQueue);
        if (freshQueue.paused) {
          setWatchPublisher(false);
          setNotice(
            freshQueue.paused_reason ??
              "YouTube scheduling paused before the next post. Check Platform connection, then resume.",
            true,
          );
          return;
        }
        if (!freshQueue.running && freshQueue.queued === 0) {
          setWatchPublisher(false);
          const remaining = freshLineup.scheduled.filter(
            (proposal) => proposal.status === "internally_scheduled",
          ).length;
          setNotice(
            remaining === 0
              ? "YouTube sync complete. Every queued Lineup post is verified."
              : `YouTube sync stopped with ${remaining} ${remaining === 1 ? "post" : "posts"} still waiting. Review the red or amber statuses before retrying.`,
            remaining > 0,
          );
          return;
        }
      } catch (error) {
        if (cancelled) return;
        setNotice(
          actionError(
            error,
            "Runway could not refresh YouTube progress. Scheduling may still be running; refresh Lineup to verify.",
            "Runway could not verify YouTube progress. Scheduling may still be running; refresh Lineup to verify.",
          ),
          true,
        );
      }
      if (!cancelled) {
        timer = window.setTimeout(refreshPublisher, 1500);
      }
    }

    timer = window.setTimeout(refreshPublisher, 700);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [publishingEnabled, publishingMode, watchPublisher]);

  const occupiedTarget = draftDate
    ? scheduledByDate.get(draftDate)
    : undefined;
  const occupiedOther = occupiedTarget?.find(
    (proposal) => proposal.id !== selected?.id,
  );
  const currentDraftError = dialog === "edit" ? draftValidation() : null;
  const displayedDialogError = dialogError || currentDraftError || "";
  const today = localDate(new Date(), lineup.timezone) ?? undefined;
  const browserMode = publishingMode === "authorized_browser";
  const queueInMotion =
    browserMode && (publisherQueue.running || publisherQueue.queued > 0);
  const pushDisabled =
    busy !== null ||
    pushOutcomeUncertain ||
    (browserMode &&
      (!publishingEnabled || queueInMotion || publisherQueue.paused)) ||
    waitingForYouTube.length === 0;
  const pushLabel =
    busy === "push"
      ? browserMode
        ? "Creating queue…"
        : "Preparing workspace…"
      : queueInMotion
        ? `Scheduling ${publisherQueue.queued || "next"}…`
        : waitingForYouTube.length > 0
          ? browserMode
            ? `Schedule ${waitingForYouTube.length} on YouTube`
            : `Prepare ${waitingForYouTube.length} for YouTube`
          : "No posts need external handling";

  return (
    <div ref={pageRef} className="lineup-page" tabIndex={-1}>
      <header className="lineup-header">
        <div>
          <p className="eyebrow">Qlob scheduler</p>
          <h1>Release calendar.</h1>
          <p className="lede">
            {lineup.coverage} upcoming{" "}
            {lineup.coverage === 1 ? "post" : "posts"} organized. One Runway post
            per local day in {displayTimezone(lineup.timezone)}. New posts start at{" "}
            {displayTime(lineup.default_time)}, and every post can use its own time.
            Published posts remain visible as locked history.
          </p>
        </div>
        <Link href="/review" className="button lineup-return">
          Back to Generator
        </Link>
      </header>

      <section
        className={[
          "lineup-youtube-bar",
          browserMode && !publishingEnabled ? "disabled" : "",
          browserMode && publisherQueue.paused ? "paused" : "",
          queueInMotion ? "working" : "",
        ]
          .filter(Boolean)
          .join(" ")}
        aria-label="External publishing"
      >
        <span className="lineup-youtube-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" focusable="false">
            <path d="M9.2 7.5 16 12l-6.8 4.5v-9Z" />
          </svg>
        </span>
        <div>
          <strong>
            {!browserMode
              ? "Assisted publishing is ready"
              : !publishingEnabled
                ? "Authorized browser publishing is off"
                : publisherQueue.paused
                ? "YouTube needs attention"
                : queueInMotion
                  ? "Scheduling through the visible browser"
                    : waitingForYouTube.length > 0
                      ? `${waitingForYouTube.length} ready for YouTube`
                      : "Lineup and YouTube are synchronized"}
          </strong>
          <span>
            {!browserMode
              ? waitingForYouTube.length > 0
                ? `Prepare an ordered workspace with exact captions, images, dates, times, and ${lineup.timezone}. Runway will not queue work or open a browser.`
                : "Accept in Generator adds posts here. Assisted preparation never marks external work verified."
              : !publishingEnabled
                ? "Restart Runway with all authorized-browser interlocks enabled before creating queue work."
                : publisherQueue.paused
                ? publisherQueue.paused_reason ??
                  "Check the publisher login before resuming."
                : queueInMotion
                  ? `${publisherQueue.queued} remaining in the serialized queue. One post is handled at a time.`
                    : waitingForYouTube.length > 0
                      ? `The explicit action queues each selected post in date order. Runway preserves one post per local day in ${lineup.timezone}.`
                      : "Every upcoming post shown as scheduled has a verified YouTube result."}
          </span>
        </div>
        <div className="lineup-youtube-actions">
          <button
            type="button"
            className="button lineup-push"
            onClick={(event) => openPublish(event.currentTarget)}
            disabled={pushDisabled}
            aria-busy={busy === "push" || queueInMotion}
          >
            {pushLabel}
          </button>
          {(pushOutcomeUncertain || publisherQueue.paused) && (
            <Link href="/settings#platform-connection">
              {pushOutcomeUncertain ? "Verify in Settings" : "Fix connection"}
            </Link>
          )}
        </div>
      </section>

      {assistedWorkspace && (
        <AssistedPublishingWorkspace
          workspace={assistedWorkspace}
          onClose={() => setAssistedWorkspace(null)}
        />
      )}

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
            Runway post. Editing and removal are locked; refresh and verify Activity
            before continuing.
          </span>
        </section>
      )}

      {allPosts.length ? (
        <div className="lineup-layout">
          <section
            className="lineup-calendar"
            aria-label="Runway release calendar"
          >
            <p id="lineup-drag-help" className="visually-hidden">
              Editable future posts can be dragged to another calendar date.
              Dropping on an occupied editable date opens a confirmed swap.
              Keyboard users can use Edit or choose date for the same action.
            </p>
            <div className="lineup-section-label">
              <strong>Calendar</strong>
              <span>Drag a post to move or swap</span>
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
                const draggedProposal = lineup.scheduled.find(
                  (item) => item.id === draggedId,
                );
                const isDropTarget =
                  dragTargetDate === key &&
                  draggedProposal !== undefined &&
                  localDate(
                    proposalSlot(draggedProposal),
                    lineup.timezone,
                  ) !== key;
                const isSwapTarget =
                  isDropTarget &&
                  proposals.some(
                    (item) =>
                      item.id !== draggedId && item.status !== "published",
                  );
                return (
                  <article
                    className={[
                      "lineup-day",
                      inMonth ? "" : "outside",
                      proposal ? "occupied" : "",
                      isDropTarget ? "drop-target" : "",
                      isSwapTarget ? "swap-target" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    key={key}
                    onDragOver={(event) => dragOverDate(event, key)}
                    onDragEnter={(event) => dragOverDate(event, key)}
                    onDrop={(event) => dropOnDate(event, key)}
                  >
                    <time dateTime={key}>{day.getUTCDate()}</time>
                    {isDropTarget && (
                      <span className="lineup-drop-label" aria-hidden="true">
                        {isSwapTarget ? "Swap" : "Move here"}
                      </span>
                    )}
                    {proposal && (
                      <button
                        type="button"
                        className={[
                          selectedId === proposal.id ? "selected" : "",
                          draggedId === proposal.id ? "dragging" : "",
                          recentlyChangedIds.includes(proposal.id)
                            ? "lineup-settled"
                            : "",
                          `status-tone-${statusTone(proposal.status)}`,
                        ]
                          .filter(Boolean)
                          .join(" ")}
                        onClick={() => select(proposal)}
                        draggable={
                          busy === null && canEditProposal(proposal)
                        }
                        onDragStart={(event) => beginDrag(event, proposal)}
                        onDragEnd={endDrag}
                        aria-describedby={
                          canEditProposal(proposal)
                            ? "lineup-drag-help"
                            : undefined
                        }
                        aria-label={`Select ${proposal.final_caption}, ${dayFormatter.format(day)}, ${statusLabel(proposal.status)}`}
                      >
                        {proposal.candidate?.preview_url && (
                          <img
                            src={`${API_URL}${proposal.candidate.preview_url}`}
                            alt=""
                            loading="lazy"
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

          <section className="lineup-agenda" aria-label="Runway release agenda">
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
              <p className="lineup-list-empty">No Runway posts this month.</p>
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
                          loading="lazy"
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
                          loading="lazy"
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
                        alt={`Selected image for “${selected.final_caption}”`}
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
                    {publishingMode === "authorized_browser" &&
                      publishingEnabled &&
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
                      ? "Confirmed changes update the local Lineup only, invalidate stale prepared work, and never open YouTube."
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
                : dialog === "remove"
                  ? "lineup-remove-copy lineup-dialog-error"
                  : "lineup-publish-copy lineup-dialog-error"
            }
            onMouseDown={(event) => event.stopPropagation()}
          >
            {dialog === "edit" ? (
              <>
                <p className="eyebrow">Confirm a Lineup change</p>
                <h2 id="lineup-dialog-title">Refine the release.</h2>
                <p id="lineup-dialog-help" className="dialog-intro">
                  Change the caption, date, or local time. The channel timezone is
                  fixed to {lineup.timezone}.
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
                    Release date
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
                <label htmlFor="lineup-time">
                  <span>Release time · {lineup.timezone}</span>
                  <input
                    id="lineup-time"
                    aria-label="Release time"
                    type="time"
                    value={draftTime}
                    onChange={(event) => {
                      setDraftTime(event.target.value);
                      setDialogError("");
                    }}
                    aria-invalid={currentDraftError ? "true" : undefined}
                    disabled={busy !== null || dialogOutcomeUncertain}
                  />
                  <small>
                    {displayTime(lineup.default_time)} is only the initial suggestion.
                  </small>
                </label>
                {occupiedOther && !currentDraftError && (
                  <p className="swap-notice">
                    <strong>Swap on confirmation.</strong> {draftDate} currently
                    holds “{occupiedOther.final_caption}” — confirming moves that
                    post to this post’s current date, so there will still be only one
                    Runway post per day.
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
            ) : dialog === "remove" ? (
              <>
                <p className="eyebrow danger">Remove from Lineup</p>
                <h2 id="lineup-dialog-title">Pull this release?</h2>
                <p id="lineup-remove-copy" className="dialog-copy">
                  This removes only the local Runway slot, invalidates stale prepared
                  work, and does not change anything on YouTube. The decision remains
                  in Activity.
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
                    {busy === "remove" ? "Removing locally…" : "Confirm remove"}
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="eyebrow">Explicit external action</p>
                <h2 id="lineup-dialog-title">
                  {publishingMode === "assisted"
                    ? "Prepare the native posting workspace?"
                    : "Queue the authorized browser publisher?"}
                </h2>
                <p id="lineup-publish-copy" className="dialog-copy">
                  Review the exact payloads below. This action uses{" "}
                  <strong>
                    {publishingMode === "assisted"
                      ? "assisted preparation"
                      : "authorized browser automation"}
                  </strong>
                  {publishingMode === "assisted"
                    ? ". It creates no queue work and opens no browser."
                    : ". Queue creation is not proof that YouTube scheduled every post."}
                </p>
                <dl className="lineup-publish-summary">
                  <div>
                    <dt>Target channel</dt>
                    <dd>{channelName}</dd>
                  </div>
                  <div>
                    <dt>Posts</dt>
                    <dd>{waitingForYouTube.length}</dd>
                  </div>
                  <div>
                    <dt>Timezone</dt>
                    <dd>{lineup.timezone}</dd>
                  </div>
                  <div>
                    <dt>Range</dt>
                    <dd>
                      {waitingForYouTube.length ? (
                        <>
                          {slotFormatter.format(
                            new Date(proposalSlot(waitingForYouTube[0])),
                          )}
                          {" – "}
                          {slotFormatter.format(
                            new Date(
                              proposalSlot(
                                waitingForYouTube[waitingForYouTube.length - 1],
                              ),
                            ),
                          )}
                        </>
                      ) : (
                        "No posts"
                      )}
                    </dd>
                  </div>
                </dl>
                <ol className="lineup-publish-items">
                  {waitingForYouTube.map((proposal) => (
                    <li key={proposal.id}>
                      {proposal.candidate?.preview_url && (
                        <img
                          src={`${API_URL}${proposal.candidate.preview_url}`}
                          alt=""
                        />
                      )}
                      <span>
                        <time>
                          {slotFormatter.format(new Date(proposalSlot(proposal)))} ·{" "}
                          {lineup.timezone}
                        </time>
                        <strong>{proposal.final_caption}</strong>
                        <small>{proposal.candidate?.original_url}</small>
                        {proposal.candidate?.rights_status === "unknown" && (
                          <small className="lineup-publish-warning">
                            Rights are unknown. Confirm that you are authorized to publish
                            this exact image before continuing.
                          </small>
                        )}
                      </span>
                    </li>
                  ))}
                </ol>
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
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="button approve"
                    onClick={pushLineupToYouTube}
                    disabled={busy !== null || pushOutcomeUncertain}
                    aria-busy={busy === "push"}
                  >
                    {busy === "push"
                      ? publishingMode === "assisted"
                        ? "Preparing…"
                        : "Creating queue…"
                      : publishingMode === "assisted"
                        ? "Confirm and prepare"
                        : "Confirm and queue"}
                  </button>
                </div>
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
