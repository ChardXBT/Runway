export const activityCategories = [
  ["all", "All activity"],
  ["decisions", "Creator decisions"],
  ["publishing", "YouTube + Lineup"],
  ["intelligence", "Intelligence"],
  ["data", "Data collection"],
  ["settings", "Settings"],
] as const;

export type ActivityCategory = (typeof activityCategories)[number][0];

export function isActivityCategory(value: string): value is ActivityCategory {
  return activityCategories.some(([category]) => category === value);
}

export function activityCategory(eventType: string): ActivityCategory {
  const value = eventType.toLowerCase();
  if (
    value.includes("youtube") ||
    value.includes("publish") ||
    value.includes("lineup") ||
    value.includes("schedule")
  ) {
    return "publishing";
  }
  if (
    value.includes("caption") ||
    value.includes("feedback") ||
    value.includes("approved") ||
    value.includes("rejected") ||
    value.includes("image_replaced")
  ) {
    return "decisions";
  }
  if (
    value.includes("intelligence") ||
    value.includes("representation") ||
    value.includes("retrieval") ||
    value.includes("profile") ||
    value.includes("diversity") ||
    value.includes("annotation")
  ) {
    return "intelligence";
  }
  if (
    value.includes("capture") ||
    value.includes("catalog") ||
    value.includes("media") ||
    value.includes("discovery") ||
    value.includes("search")
  ) {
    return "data";
  }
  if (value.includes("settings") || value.includes("policy")) {
    return "settings";
  }
  return "intelligence";
}

export function activityTitle(eventType: string) {
  const titles: Record<string, string> = {
    proposal_approved: "Post accepted",
    proposal_rejected: "Post rejected",
    proposal_image_replaced: "Image replaced",
    caption_edited: "Caption edited",
    caption_feedback_recorded: "Preference saved",
    youtube_publish_queued: "YouTube action queued",
    youtube_submission_started: "YouTube scheduling started",
    youtube_schedule_verified: "YouTube schedule verified",
    youtube_submission_unverified: "YouTube result needs verification",
    youtube_submission_failed: "YouTube scheduling failed",
    youtube_queue_blocked: "YouTube queue paused",
    youtube_session_validated: "Publisher connection verified",
    youtube_session_validation_failed: "Publisher connection failed",
    lineup_post_updated: "Lineup post updated",
    lineup_post_removed: "Lineup post removed",
    settings_updated: "Settings updated",
    candidate_diversity_backfilled: "Image diversity refreshed",
    public_image_policy_configured: "Image policy updated",
  };
  return (
    titles[eventType] ??
    eventType
      .replaceAll("_", " ")
      .replace(/^\w/, (character) => character.toUpperCase())
  );
}

export function activitySummary(
  eventType: string,
  details: Record<string, unknown>,
) {
  if (eventType === "proposal_approved") {
    return "Runway saved the accepted image-caption pairing and reserved its daily slot.";
  }
  if (eventType === "proposal_rejected") {
    return "Runway saved a negative image, caption, and pairing signal for future ranking.";
  }
  if (eventType === "caption_edited") {
    return "The edited wording became a preferred caption example.";
  }
  if (eventType === "caption_feedback_recorded") {
    const reasons = Array.isArray(details.reason_codes)
      ? details.reason_codes.filter((value) => typeof value === "string")
      : [];
    return reasons.length
      ? `Saved learning reasons: ${reasons.join(", ").replaceAll("_", " ")}.`
      : "Saved creator feedback for future retrieval and ranking.";
  }
  if (eventType === "proposal_image_replaced") {
    return "The previous image was marked down and a new candidate entered review.";
  }
  if (eventType === "youtube_schedule_verified") {
    return "The scheduled post was found on YouTube and the local record was synchronized.";
  }
  if (eventType === "youtube_session_validated") {
    return "The saved publisher identity, channel access, and Community controls passed.";
  }
  if (eventType === "youtube_session_validation_failed") {
    return "Runway could not prove the saved publisher session was ready.";
  }
  if (eventType === "youtube_queue_blocked") {
    return "Publishing stopped before another post was submitted; repair the connection before resuming.";
  }
  if (eventType === "lineup_post_updated") {
    return details.swapped
      ? "Two occupied daily slots were swapped and synchronized."
      : "The scheduled caption or release date was updated.";
  }
  if (eventType === "lineup_post_removed") {
    return "The future post was removed only after the external state was handled safely.";
  }
  if (eventType === "settings_updated") {
    return "Local channel rules changed; the previous and new values remain in the technical record.";
  }
  return "Runway preserved this operation in the immutable local audit trail.";
}

export function activityTeachesModel(eventType: string) {
  return new Set([
    "proposal_approved",
    "proposal_rejected",
    "proposal_image_replaced",
    "caption_edited",
    "caption_feedback_recorded",
  ]).has(eventType);
}
