import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LineupSchedule, Proposal } from "@/lib/types";
import { LineupCalendar } from "./lineup-calendar";

const first: Proposal = {
  id: 12,
  generation_run_id: 1,
  planned_publish_at: "2026-08-08T10:00:00-04:00",
  scheduled_publish_at: "2026-08-08T10:00:00-04:00",
  status: "internally_scheduled",
  candidate_image_id: 7,
  backup_candidate_ids: [],
  recommended_caption: "First line",
  alternative_captions: [],
  caption_rationale: "",
  caption_confidence: 0.9,
  caption_reference_post_ids: [],
  factual_uncertainty_warning: null,
  final_caption: "First line",
  selection_reason: "",
  scores: { style: 0.8, novelty: 0.7, quality: 0.9 },
  closest_historical_matches: [],
  warnings: [],
  rights_decision: null,
  rights_reviewed_at: null,
  external_post_id: null,
  external_post_url: null,
  scheduled_verified_at: null,
  candidate: {
    original_url: "/media/first.jpg",
    preview_url: "/media/first.jpg",
    source_page_url: null,
    direct_image_url: null,
    source_domain: null,
    rights_status: "unknown",
    detected_topic: {},
  },
};

const second: Proposal = {
  ...first,
  id: 13,
  planned_publish_at: "2026-08-09T10:00:00-04:00",
  scheduled_publish_at: "2026-08-09T10:00:00-04:00",
  recommended_caption: "Second line",
  final_caption: "Second line",
};

const lineup: LineupSchedule = {
  timezone: "America/Toronto",
  default_time: "10:00",
  posts_per_day: 1,
  coverage: 2,
  next_available_at: "2026-08-10T10:00:00-04:00",
  scheduled: [first, second],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("LineupCalendar", () => {
  it("confirms punctuation-preserving edits and occupied-date swaps", async () => {
    const response = { lineup };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => response,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "Edit or move" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Caption"), {
      target: { value: "Wait... what?!" },
    });
    fireEvent.change(screen.getByLabelText(/Release date/), {
      target: { value: "2026-08-09" },
    });

    expect(screen.getByText(/swaps the two release dates/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm changes" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      final_caption: "Wait... what?!",
      new_date: "2026-08-09",
      confirmed: true,
    });
  });

  it("requires a second explicit confirmation before removal", async () => {
    const empty = { ...lineup, coverage: 0, scheduled: [] };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ lineup: empty }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm remove" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain("/api/lineup/12/remove");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      confirmed: true,
    });
  });

  it("preserves the exact slot when only the caption changes", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ lineup }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "Edit or move" }));
    fireEvent.change(screen.getByLabelText("Caption"), {
      target: { value: "Why now?!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm changes" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      final_caption: "Why now?!",
      new_date: null,
      confirmed: true,
    });
  });

  it("closes an unconfirmed dialog with Escape", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "Edit or move" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the calendar and upcoming-post list together", () => {
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);

    expect(screen.getByLabelText("RunWay release calendar")).toBeInTheDocument();
    expect(screen.getByLabelText("Upcoming posts")).toHaveTextContent("First line");
    expect(screen.getByLabelText("Upcoming posts")).toHaveTextContent("Second line");
  });

  it("retries a waiting YouTube action from the selected post", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ lineup }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "Retry YouTube" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain("/api/lineup/12/retry");
  });
});
