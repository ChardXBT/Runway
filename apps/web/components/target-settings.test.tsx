import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  InstagramReelSettings,
  TargetSettingsPanel,
  YouTubeVideoSettings,
} from "./target-settings";

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("YouTubeVideoSettings", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads categories and leaves the required selects unchosen", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        items: [
          { id: "10", title: "Music", assignable: true },
          { id: "22", title: "People & Blogs", assignable: true },
        ],
        region: "US",
      }),
    );
    render(
      <YouTubeVideoSettings accountId={5} settings={{}} onChange={vi.fn()} />,
    );

    await waitFor(() =>
      expect(screen.getByRole("option", { name: "Music" })).toBeInTheDocument(),
    );
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("/api/v1/accounts/5/youtube/categories");

    // Privacy / kids / placement / scheduling are required with no default.
    expect((screen.getByLabelText("Who can see it") as HTMLSelectElement).value).toBe("");
    expect((screen.getByLabelText("Made for kids?") as HTMLSelectElement).value).toBe("");
    expect((screen.getByLabelText("Paid product placement?") as HTMLSelectElement).value).toBe("");
    expect((screen.getByLabelText("Scheduling") as HTMLSelectElement).value).toBe("");
    expect((screen.getByLabelText("Category") as HTMLSelectElement).value).toBe("");
  });

  it("emits each choice as a settings patch", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], region: "US" }));
    const onChange = vi.fn();
    render(
      <YouTubeVideoSettings accountId={5} settings={{}} onChange={onChange} />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Who can see it"), {
      target: { value: "unlisted" },
    });
    expect(onChange).toHaveBeenCalledWith({ privacy: "unlisted" });

    fireEvent.change(screen.getByLabelText("Made for kids?"), {
      target: { value: "no" },
    });
    expect(onChange).toHaveBeenCalledWith({ made_for_kids: false });
  });

  it("falls back to a category id input when the categories route fails", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "Categories unavailable" }, 404),
    );
    render(
      <YouTubeVideoSettings accountId={5} settings={{}} onChange={vi.fn()} />,
    );
    await waitFor(() =>
      expect(screen.getByText("Categories unavailable")).toBeInTheDocument(),
    );
    expect(screen.getByPlaceholderText("Category id")).toBeInTheDocument();
  });
});

describe("InstagramReelSettings", () => {
  afterEach(cleanup);

  it("renders reel fields and emits share_to_feed", () => {
    const onChange = vi.fn();
    render(<InstagramReelSettings settings={{}} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText("Also share to feed?"), {
      target: { value: "yes" },
    });
    expect(onChange).toHaveBeenCalledWith({ share_to_feed: true });
  });
});

describe("TargetSettingsPanel", () => {
  afterEach(cleanup);

  it("dispatches to the right panel per post type", () => {
    const { rerender } = render(
      <TargetSettingsPanel
        postType="instagram_story"
        accountId={null}
        platform="instagram"
        settings={{}}
        onChange={vi.fn()}
        captionLength={0}
        media={[]}
      />,
    );
    expect(screen.getByText(/media is the Story/)).toBeInTheDocument();

    rerender(
      <TargetSettingsPanel
        postType={null}
        accountId={null}
        platform="instagram"
        settings={{}}
        onChange={vi.fn()}
        captionLength={0}
        media={[]}
      />,
    );
    expect(screen.queryByText(/media is the Story/)).not.toBeInTheDocument();
  });
});
