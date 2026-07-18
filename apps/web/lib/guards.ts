import type {
  EditorialEnvelope,
  LineupSchedule,
  Proposal,
  PublisherQueueStatus,
  WorkflowStatus,
} from "@/lib/types";
import { isValidTimeZone, localDate } from "@/lib/datetime";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isDateString(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    !Number.isNaN(new Date(value).getTime())
  );
}

export function isProposal(value: unknown): value is Proposal {
  if (!isRecord(value)) return false;
  const candidateValid =
    value.candidate === null ||
    (isRecord(value.candidate) &&
      (value.candidate.preview_url === null ||
        typeof value.candidate.preview_url === "string") &&
      isRecord(value.candidate.detected_topic) &&
      (value.candidate.detected_topic.franchise === undefined ||
        value.candidate.detected_topic.franchise === null ||
        typeof value.candidate.detected_topic.franchise === "string") &&
      (value.candidate.detected_topic.characters === undefined ||
        (Array.isArray(value.candidate.detected_topic.characters) &&
          value.candidate.detected_topic.characters.every(
            (item) => typeof item === "string",
          ))));
  return (
    isFiniteNumber(value.id) &&
    isFiniteNumber(value.generation_run_id) &&
    isDateString(value.planned_publish_at) &&
    (value.scheduled_publish_at === null ||
      isDateString(value.scheduled_publish_at)) &&
    typeof value.status === "string" &&
    isFiniteNumber(value.candidate_image_id) &&
    typeof value.final_caption === "string" &&
    typeof value.recommended_caption === "string" &&
    Array.isArray(value.alternative_captions) &&
    value.alternative_captions.every((item) => typeof item === "string") &&
    candidateValid
  );
}

export function isWorkflowStatus(value: unknown): value is WorkflowStatus {
  if (!isRecord(value)) return false;
  return (
    isFiniteNumber(value.needs_review) &&
    isFiniteNumber(value.queued) &&
    isFiniteNumber(value.scheduled) &&
    isFiniteNumber(value.rejected) &&
    isDateString(value.next_available_at) &&
    isFiniteNumber(value.posts_per_day) &&
    typeof value.timezone === "string" &&
    isValidTimeZone(value.timezone)
  );
}

export function isPublisherQueueStatus(
  value: unknown,
): value is PublisherQueueStatus {
  if (!isRecord(value)) return false;
  return (
    typeof value.running === "boolean" &&
    isFiniteNumber(value.queued) &&
    Number.isInteger(value.queued) &&
    value.queued >= 0 &&
    typeof value.paused === "boolean" &&
    !(value.running && value.paused) &&
    (value.paused_reason === null || typeof value.paused_reason === "string")
  );
}

export function isEditorialEnvelope(value: unknown): value is EditorialEnvelope {
  if (!isRecord(value)) return false;
  return (
    (value.next_proposal === null || isProposal(value.next_proposal)) &&
    isWorkflowStatus(value.workflow) &&
    (value.proposal === undefined || isProposal(value.proposal)) &&
    (value.publisher_queue === undefined ||
      isPublisherQueueStatus(value.publisher_queue))
  );
}

export function isLineupSchedule(value: unknown): value is LineupSchedule {
  if (!isRecord(value)) return false;
  return (
    typeof value.timezone === "string" &&
    isValidTimeZone(value.timezone) &&
    typeof value.default_time === "string" &&
    /^([01]\d|2[0-3]):[0-5]\d$/.test(value.default_time) &&
    isFiniteNumber(value.posts_per_day) &&
    value.posts_per_day === 1 &&
    isFiniteNumber(value.coverage) &&
    isDateString(value.next_available_at) &&
    Array.isArray(value.scheduled) &&
    value.scheduled.every(isProposal) &&
    value.coverage === value.scheduled.length
  );
}

export function proposalList(value: unknown): value is Proposal[] {
  return Array.isArray(value) && value.every(isProposal);
}

export function conflictingLineupDates(lineup: LineupSchedule) {
  const counts = new Map<string, number>();
  for (const proposal of lineup.scheduled) {
    const slot = proposal.scheduled_publish_at ?? proposal.planned_publish_at;
    const date = localDate(slot, lineup.timezone);
    if (!date) return ["invalid-date"];
    counts.set(date, (counts.get(date) ?? 0) + 1);
  }
  return [...counts.entries()]
    .filter(([, count]) => count > 1)
    .map(([date]) => date);
}
