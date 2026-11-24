/** Plain-language labels for post/target state (spec 08: no jargon). */

export const POST_TYPE_LABELS: Record<string, string> = {
  youtube_video: "YouTube video",
  youtube_short: "YouTube Short",
  youtube_post: "YouTube community post",
  instagram_reel: "Instagram Reel",
  instagram_image: "Instagram photo",
  instagram_carousel: "Instagram carousel",
  instagram_story: "Instagram Story",
  tiktok_video: "TikTok video",
  tiktok_draft: "TikTok inbox draft",
};

export function postTypeLabel(postType: string | null | undefined): string {
  if (!postType) return "a post";
  return POST_TYPE_LABELS[postType] ?? postType.replace(/_/g, " ");
}

/** Post types the UI offers per platform, in menu order. */
export const POST_TYPES_BY_PLATFORM: Record<string, string[]> = {
  youtube: ["youtube_video", "youtube_short", "youtube_post"],
  instagram: [
    "instagram_reel",
    "instagram_image",
    "instagram_carousel",
    "instagram_story",
  ],
  tiktok: ["tiktok_video", "tiktok_draft"],
};

export function postTypesForPlatform(platform: string): string[] {
  return POST_TYPES_BY_PLATFORM[platform] ?? [];
}

export interface StateCopy {
  label: string;
  detail: string;
  tone: "neutral" | "progress" | "success" | "warning" | "danger";
}

const STATE_COPY: Record<string, StateCopy> = {
  draft: {
    label: "Draft",
    detail: "Still being prepared — nothing is scheduled or sent.",
    tone: "neutral",
  },
  ready: {
    label: "Ready",
    detail: "Ready to schedule or release.",
    tone: "neutral",
  },
  released: {
    label: "Released",
    detail: "Approved; waiting for its scheduled time.",
    tone: "progress",
  },
  queued: {
    label: "Queued",
    detail: "Waiting in the publish queue for its turn.",
    tone: "progress",
  },
  sending: {
    label: "Sending",
    detail: "Being sent to the platform now.",
    tone: "progress",
  },
  processing: {
    label: "Processing",
    detail: "The platform has it and is still processing (for example a video encode).",
    tone: "progress",
  },
  published: {
    label: "Published",
    detail: "Live on the platform.",
    tone: "success",
  },
  verified: {
    label: "Published — verified",
    detail: "Runway confirmed it is live on the platform.",
    tone: "success",
  },
  failed_retryable: {
    label: "Will retry",
    detail: "The send failed in a way that usually recovers; Runway retries it.",
    tone: "warning",
  },
  failed_final: {
    label: "Failed",
    detail: "The send failed for good; the reason is shown below.",
    tone: "danger",
  },
  uncertain: {
    label: "Uncertain",
    detail:
      "The platform's answer timed out or was unclear, so Runway does not know whether it went live. Check before retrying.",
    tone: "warning",
  },
  needs_user: {
    label: "Needs you",
    detail: "A step only you can do is waiting — usually finishing in the TikTok app.",
    tone: "warning",
  },
  cancelled: {
    label: "Cancelled",
    detail: "Withdrawn before it was sent.",
    tone: "neutral",
  },
};

export function targetStateCopy(state: string): StateCopy {
  return STATE_COPY[state] ?? { label: state.replace(/_/g, " "), detail: "", tone: "neutral" };
}

const POST_STATUS_COPY: Record<string, StateCopy> = {
  draft: { label: "Draft", detail: "Nothing scheduled or sent yet.", tone: "neutral" },
  scheduled: {
    label: "Scheduled",
    detail: "At least one target waits for its time.",
    tone: "progress",
  },
  publishing: {
    label: "Publishing",
    detail: "Sending is in progress.",
    tone: "progress",
  },
  published: {
    label: "Published",
    detail: "Every target finished.",
    tone: "success",
  },
  attention: {
    label: "Needs attention",
    detail: "At least one target needs you.",
    tone: "warning",
  },
};

export function postStatusCopy(status: string): StateCopy {
  return POST_STATUS_COPY[status] ?? { label: status.replace(/_/g, " "), detail: "", tone: "neutral" };
}

export const CAPABILITY_STATE_COPY: Record<string, StateCopy> = {
  ready: { label: "Ready", detail: "Direct posting is available.", tone: "success" },
  ready_draft_only: {
    label: "Drafts only",
    detail: "Can send drafts the creator finishes in the app.",
    tone: "progress",
  },
  needs_scope: {
    label: "Enable publishing",
    detail: "The account lacks the publishing permission; a short consent adds it.",
    tone: "warning",
  },
  needs_app_approval: {
    label: "App review needed",
    detail: "The developer app needs platform approval first.",
    tone: "warning",
  },
  needs_media_host: {
    label: "Media host needed",
    detail: "Media must be reachable by a public URL for this post type.",
    tone: "warning",
  },
  assisted_only: {
    label: "Assisted only",
    detail: "No direct adapter in this build; use the assisted checklist.",
    tone: "neutral",
  },
};

export function capabilityStateCopy(state: string): StateCopy {
  return CAPABILITY_STATE_COPY[state] ?? { label: state.replace(/_/g, " "), detail: "", tone: "neutral" };
}

/** `IssueOut.severity` → the notice tone the design system already has. */
export function issueTone(severity: string): "error" | "warning" | "info" {
  return severity === "error" ? "error" : severity === "warning" ? "warning" : "info";
}
