import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
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
  vi.useRealTimers();
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
    fireEvent.click(
      screen.getByRole("button", { name: "Edit or choose date" }),
    );
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Caption"), {
      target: { value: "Wait... what?!" },
    });
    fireEvent.change(screen.getByLabelText(/Release date/), {
      target: { value: "2026-08-09" },
    });

    expect(screen.getByText("Swap on confirmation.")).toBeInTheDocument();
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
    fireEvent.click(
      screen.getByRole("button", { name: "Edit or choose date" }),
    );
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
    const opener = screen.getByRole("button", { name: "Edit or choose date" });
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog");
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("shows the calendar and upcoming-post list together", () => {
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);

    expect(screen.getByLabelText("Runway release calendar")).toBeInTheDocument();
    expect(screen.getByLabelText("Runway release agenda")).toBeInTheDocument();
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

  it("pushes every new Lineup post once and locks duplicate clicks", async () => {
    let resolveResponse!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      resolveResponse = resolve;
    });
    const fetchMock = vi.fn().mockReturnValue(pending);
    vi.stubGlobal("fetch", fetchMock);
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);

    const push = screen.getByRole("button", {
      name: "Push 2 to YouTube",
    });
    fireEvent.click(push);
    fireEvent.click(push);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain("/api/lineup/push");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      confirmed: true,
    });
    expect(
      screen.getByRole("button", { name: "Checking session…" }),
    ).toBeDisabled();

    resolveResponse({
      ok: true,
      json: async () => ({
        detail: "YouTube is already up to date.",
        queued_proposal_ids: [],
        lineup,
        publisher_queue: {
          running: false,
          queued: 0,
          paused: false,
          paused_reason: null,
        },
      }),
    });
    await waitFor(() =>
      expect(screen.getByText("YouTube is already up to date.")).toBeInTheDocument(),
    );
  });

  it("never claims a malformed YouTube push succeeded and requires refresh", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ queued_proposal_ids: [12, 13] }),
      }),
    );
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);

    fireEvent.click(
      screen.getByRole("button", { name: "Push 2 to YouTube" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "could not verify the YouTube queue response",
    );
    expect(
      screen.getByRole("link", { name: "Verify in Settings" }),
    ).toHaveAttribute("href", "/settings#platform-connection");
    expect(
      screen.getByRole("button", { name: "Push 2 to YouTube" }),
    ).toBeDisabled();
    expect(screen.queryByText(/sync complete/i)).not.toBeInTheDocument();
  });

  it("turns an occupied drag target into a confirmed animated swap", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ lineup }),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);
    const calendar = screen.getByLabelText("Runway release calendar");
    const source = within(calendar).getByRole("button", {
      name: /Select First line/,
    });
    const target = within(calendar)
      .getByRole("button", { name: /Select Second line/ })
      .closest("article");
    expect(source).toHaveAttribute("draggable", "true");
    expect(target).not.toBeNull();
    const dataTransfer = {
      effectAllowed: "",
      dropEffect: "",
      setData: vi.fn(),
      getData: vi.fn(),
    };

    fireEvent.dragStart(source, { dataTransfer });
    fireEvent.dragEnter(target!, { dataTransfer });
    expect(within(target!).getByText("Swap")).toBeInTheDocument();
    fireEvent.drop(target!, { dataTransfer });

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Swap on confirmation.")).toBeInTheDocument();
    expect(screen.getByLabelText("Release date")).toHaveValue("2026-08-09");
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Confirm changes" }),
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(source).toHaveClass("lineup-settled");
    expect(
      within(calendar).getByRole("button", { name: /Select Second line/ }),
    ).toHaveClass("lineup-settled");
  });

  it("opens a clear swap confirmation from the one-day shortcut", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);

    fireEvent.click(screen.getByRole("button", { name: "One day →" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Swap on confirmation.")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("there will still be only one Runway post per day");
    expect(screen.getByLabelText("Release date")).toHaveValue("2026-08-09");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("prevents a Toronto slot in the past and explains why", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-18T14:30:00Z"));
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);

    fireEvent.click(
      screen.getByRole("button", { name: "Edit or choose date" }),
    );
    fireEvent.change(screen.getByLabelText("Release date"), {
      target: { value: "2026-07-18" },
    });

    expect(screen.getByLabelText("Release date")).toHaveAttribute(
      "min",
      "2026-07-18",
    );
    expect(screen.getByLabelText("Release date")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByRole("button", { name: "Confirm changes" })).toBeDisabled();
    expect(within(screen.getByRole("dialog")).getByRole("alert")).toHaveTextContent(
      "Choose a future 10:00 AM Eastern slot",
    );
  });

  it("uses the configured timezone for same-day validation", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-18T16:30:00Z"));
    const pacific = {
      ...lineup,
      timezone: "America/Vancouver",
    };
    render(<LineupCalendar initialLineup={pacific} publishingEnabled />);

    fireEvent.click(
      screen.getByRole("button", { name: "Edit or choose date" }),
    );
    fireEvent.change(screen.getByLabelText("Release date"), {
      target: { value: "2026-07-18" },
    });

    expect(screen.getByRole("button", { name: "Confirm changes" })).toBeEnabled();
    expect(screen.getByLabelText("Release date")).not.toHaveAttribute(
      "aria-invalid",
    );
  });

  it("locks duplicate confirmations while a Lineup mutation is running", async () => {
    let resolveResponse!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      resolveResponse = resolve;
    });
    const fetchMock = vi.fn().mockReturnValue(pending);
    vi.stubGlobal("fetch", fetchMock);
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);

    fireEvent.click(
      screen.getByRole("button", { name: "Edit or choose date" }),
    );
    const confirm = screen.getByRole("button", { name: "Confirm changes" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Verifying change…" }),
    ).toBeDisabled();

    resolveResponse({
      ok: true,
      json: async () => ({ lineup }),
    });
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });

  it("preserves dialog input and blocks retry after a malformed success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ lineup: "unknown" }),
      }),
    );
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);
    fireEvent.click(
      screen.getByRole("button", { name: "Edit or choose date" }),
    );
    fireEvent.change(screen.getByLabelText("Caption"), {
      target: { value: "Wait... keep this?!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm changes" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "unexpected response",
    );
    expect(screen.getByLabelText("Caption")).toHaveValue("Wait... keep this?!");
    expect(
      screen.getByRole("button", { name: "Confirm changes" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("link", { name: "Refresh Lineup to verify" }),
    ).toHaveAttribute("href", "/lineup");
    expect(screen.queryByText(/Confirmed\./)).not.toBeInTheDocument();
  });

  it("uses labeled green, red, and amber states and locks unsafe posts", () => {
    const failed = {
      ...first,
      id: 21,
      status: "publish_failed",
      planned_publish_at: "2026-08-10T10:00:00-04:00",
      scheduled_publish_at: "2026-08-10T10:00:00-04:00",
      final_caption: "Failed post",
    };
    const unverified = {
      ...first,
      id: 22,
      status: "publish_unverified",
      planned_publish_at: "2026-08-11T10:00:00-04:00",
      scheduled_publish_at: "2026-08-11T10:00:00-04:00",
      final_caption: "Unverified post",
    };
    const publishing = {
      ...first,
      id: 23,
      status: "publishing",
      planned_publish_at: "2026-08-12T10:00:00-04:00",
      scheduled_publish_at: "2026-08-12T10:00:00-04:00",
      final_caption: "Publishing post",
    };
    const published = {
      ...first,
      id: 24,
      status: "published",
      planned_publish_at: "2026-07-10T10:00:00-04:00",
      scheduled_publish_at: "2026-07-10T10:00:00-04:00",
      final_caption: "Published post",
    };
    const states = {
      ...lineup,
      coverage: 3,
      scheduled: [failed, unverified, publishing],
    };
    render(
      <LineupCalendar
        initialLineup={states}
        initialPublished={[published]}
        publishingEnabled
      />,
    );

    const inspector = screen.getByLabelText("Selected post");
    expect(within(inspector).getByText("Failed · retry required")).toHaveClass(
      "status-tone-danger",
    );

    fireEvent.click(
      within(screen.getByLabelText("Upcoming posts")).getByRole("button", {
        name: /Unverified post/,
      }),
    );
    expect(
      within(inspector).getByText("Unverified · check required"),
    ).toHaveClass("status-tone-warning");
    expect(
      within(inspector).getByRole("button", { name: "Edit or choose date" }),
    ).toBeDisabled();
    expect(within(inspector).getByRole("button", { name: "Remove" })).toBeDisabled();

    fireEvent.click(
      within(screen.getByLabelText("Upcoming posts")).getByRole("button", {
        name: /Publishing post/,
      }),
    );
    expect(within(inspector).getByText("Publishing now")).toHaveClass(
      "status-tone-warning",
    );
    expect(
      within(inspector).getByRole("button", { name: "Edit or choose date" }),
    ).toBeDisabled();
    expect(within(inspector).getByRole("button", { name: "Remove" })).toBeDisabled();

    fireEvent.click(
      within(screen.getByLabelText("Past published posts")).getByRole("button", {
        name: /Published post/,
      }),
    );
    expect(within(inspector).getByText("Published")).toHaveClass(
      "status-tone-success",
    );
    expect(
      within(inspector).getByRole("button", { name: "Edit or choose date" }),
    ).toBeDisabled();
    expect(within(inspector).getByRole("button", { name: "Remove" })).toBeDisabled();
    expect(inspector).toHaveTextContent("locked history");
  });

  it("locks mutations when malformed state contains two active posts on one date", () => {
    const collision = {
      ...second,
      scheduled_publish_at: first.scheduled_publish_at,
      planned_publish_at: first.planned_publish_at,
    };
    render(
      <LineupCalendar
        initialLineup={{ ...lineup, scheduled: [first, collision] }}
        publishingEnabled
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Daily-slot conflict detected",
    );
    expect(
      screen.getByRole("button", { name: "Edit or choose date" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove" })).toBeDisabled();
  });

  it("rejects a successful response that would create a daily collision", async () => {
    const collision = {
      ...second,
      scheduled_publish_at: first.scheduled_publish_at,
      planned_publish_at: first.planned_publish_at,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          lineup: { ...lineup, scheduled: [first, collision] },
        }),
      }),
    );
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);
    fireEvent.click(
      screen.getByRole("button", { name: "Edit or choose date" }),
    );
    fireEvent.change(screen.getByLabelText("Caption"), {
      target: { value: "Keep this unique?!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm changes" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "conflicting daily slots",
    );
    expect(
      within(dialog).getByRole("link", { name: "Refresh Lineup to verify" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Confirmed\./)).not.toBeInTheDocument();
  });

  it("traps Tab inside the dialog", () => {
    render(<LineupCalendar initialLineup={lineup} publishingEnabled />);
    fireEvent.click(
      screen.getByRole("button", { name: "Edit or choose date" }),
    );
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", {
      name: "Confirm changes",
    });
    confirm.focus();
    fireEvent.keyDown(confirm, { key: "Tab" });

    expect(screen.getByLabelText("Caption")).toHaveFocus();
  });
});
