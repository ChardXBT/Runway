import { describe, expect, it } from "vitest";

import {
  activityCategory,
  activitySummary,
  activityTeachesModel,
  activityTitle,
} from "./activity";

describe("activity presentation", () => {
  it("separates creator learning from publishing operations", () => {
    expect(activityCategory("proposal_rejected")).toBe("decisions");
    expect(activityCategory("youtube_schedule_verified")).toBe("publishing");
    expect(activityCategory("candidate_diversity_backfilled")).toBe(
      "intelligence",
    );
    expect(activityTeachesModel("caption_edited")).toBe(true);
    expect(activityTeachesModel("youtube_schedule_verified")).toBe(false);
  });

  it("turns raw events into useful, truthful copy", () => {
    expect(activityTitle("youtube_session_validation_failed")).toBe(
      "Publisher connection failed",
    );
    expect(
      activitySummary("caption_feedback_recorded", {
        reason_codes: ["too_similar", "not_engaging"],
      }),
    ).toContain("too similar, not engaging");
    expect(activitySummary("lineup_post_updated", { swapped: true })).toContain(
      "swapped",
    );
  });
});
