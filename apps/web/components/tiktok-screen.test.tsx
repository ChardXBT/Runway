import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TikTokScreen, tiktokScreenIssues } from "./tiktok-screen";

const creatorInfo = {
  account_id: 9,
  creator_nickname: "Northwind",
  creator_username: "northwindstudio",
  creator_avatar_url: null,
  privacy_level_options: ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
  comment_disabled: false,
  duet_disabled: true,
  stitch_disabled: false,
  max_video_post_duration_sec: 600,
  consent: {
    default: "By posting, you agree to TikTok's terms.",
    branded: "Branded content needs the music usage rights.",
  },
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("tiktokScreenIssues", () => {
  it("blocks the post until a privacy level is chosen", () => {
    expect(tiktokScreenIssues({})).toEqual([
      "Choose who can see the video — TikTok requires the creator to pick it.",
    ]);
    expect(tiktokScreenIssues({ privacy_level: "PUBLIC_TO_EVERYONE" })).toEqual([]);
  });

  it("requires a commercial kind when commercial content is on", () => {
    const issues = tiktokScreenIssues({
      privacy_level: "PUBLIC_TO_EVERYONE",
      commercial_content: true,
    });
    expect(issues).toHaveLength(1);
    expect(issues[0]).toContain("promotes yourself, a third party, or both");
  });

  it("forbids SELF_ONLY on branded content", () => {
    const issues = tiktokScreenIssues({
      privacy_level: "SELF_ONLY",
      commercial_content: true,
      branded_content: true,
    });
    expect(issues).toContain("Branded content visibility cannot be set to private.");
  });
});

describe("TikTokScreen", () => {
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

  it("fetches creator options fresh, keeps privacy unchosen, and shows interactions off", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(creatorInfo));
    const onChange = vi.fn();
    render(
      <TikTokScreen
        accountId={9}
        settings={{}}
        onChange={onChange}
        captionLength={12}
        aiGenerated={false}
      />,
    );

    await waitFor(() => expect(screen.getByText("Northwind")).toBeInTheDocument());
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("http://127.0.0.1:8000/api/v1/accounts/9/tiktok/creator-info");

    // Privacy is never preselected: the empty choice is the current value.
    const privacy = screen.getByLabelText("Who can see it") as HTMLSelectElement;
    expect(privacy.value).toBe("");
    // …and the missing choice is listed as a blocking issue.
    expect(
      screen.getByText("Choose who can see the video — TikTok requires the creator to pick it."),
    ).toBeInTheDocument();

    // Interactions start off; a creator-disabled one is greyed out.
    const comments = screen.getByLabelText(/Allow comments/) as HTMLInputElement;
    const duets = screen.getByLabelText(/Allow duets/) as HTMLInputElement;
    expect(comments.checked).toBe(false);
    expect(duets.checked).toBe(false);
    expect(duets).toBeDisabled();
    expect(screen.getByText("(turned off by this creator)")).toBeInTheDocument();
  });

  it("emits the chosen privacy through onChange", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(creatorInfo));
    const onChange = vi.fn();
    render(
      <TikTokScreen
        accountId={9}
        settings={{}}
        onChange={onChange}
        captionLength={0}
        aiGenerated={false}
      />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Who can see it")).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByLabelText("Who can see it"), {
      target: { value: "SELF_ONLY" },
    });
    expect(onChange).toHaveBeenCalledWith({ privacy_level: "SELF_ONLY" });
  });

  it("says the creator options are required when the load fails", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500));
    render(
      <TikTokScreen
        accountId={9}
        settings={{}}
        onChange={vi.fn()}
        captionLength={0}
        aiGenerated={false}
      />,
    );
    // Without creator options the screen cannot prepare a video — it fails
    // closed with an alert instead of guessing.
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.queryByLabelText("Who can see it")).not.toBeInTheDocument();
  });
});
