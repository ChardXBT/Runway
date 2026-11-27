import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AssistedPublishingWorkspace as Workspace } from "@/lib/types";
import { AssistedPublishingWorkspace } from "./assisted-publishing-workspace";

const workspace: Workspace = {
  mode: "assisted",
  channel_name: "Qlob",
  channel_id: "UCxxxxxxxxxxxxxxxxxxxxxx",
  timezone: "America/Toronto",
  youtube_url: "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx/posts",
  items: [
    {
      proposal_id: 42,
      planned_publish_at: "2026-08-08T10:00:00-04:00",
      caption: "Exact caption?!",
      image_url: "/media/approved/example.png",
      rights_status: "unknown",
      warnings: [],
    },
  ],
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("AssistedPublishingWorkspace", () => {
  it("downloads the exact image through a blob rather than relying on cross-origin download", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const createObjectURL = vi.fn().mockReturnValue("blob:runway-image");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        blob: async () => new Blob(["image"], { type: "image/png" }),
      }),
    );

    render(<AssistedPublishingWorkspace workspace={workspace} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Download exact image" }));

    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    await waitFor(
      () => expect(revokeObjectURL).toHaveBeenCalledWith("blob:runway-image"),
      { timeout: 3000 },
    );
    expect(screen.getByRole("button", { name: "Image downloaded" })).toBeEnabled();
  });

  it("renders generated schedule fixtures in each synthetic workspace timezone across DST", () => {
    for (const fixture of [
      {
        timezone: "America/Toronto",
        plannedPublishAt: "2026-03-08T17:00:00",
        expectedDate: "2026-03-08",
        expectedTime: "13:00",
      },
      {
        timezone: "America/Phoenix",
        plannedPublishAt: "2026-03-08T17:00:00",
        expectedDate: "2026-03-08",
        expectedTime: "10:00",
      },
      {
        timezone: "America/Toronto",
        plannedPublishAt: "2026-11-01T17:00:00",
        expectedDate: "2026-11-01",
        expectedTime: "12:00",
      },
      {
        timezone: "America/Phoenix",
        plannedPublishAt: "2026-11-01T17:00:00",
        expectedDate: "2026-11-01",
        expectedTime: "10:00",
      },
    ]) {
      const view = render(
        <AssistedPublishingWorkspace
          workspace={{
            ...workspace,
            timezone: fixture.timezone,
            items: [
              {
                ...workspace.items[0],
                planned_publish_at: fixture.plannedPublishAt,
              },
            ],
          }}
          onClose={vi.fn()}
        />,
      );

      expect(screen.getByText(fixture.expectedDate)).toBeInTheDocument();
      expect(screen.getByText(fixture.expectedTime)).toBeInTheDocument();
      expect(screen.getByText(fixture.timezone)).toBeInTheDocument();
      view.unmount();
    }
  });

  it("renders an empty state instead of crashing when no items were prepared", () => {
    const onClose = vi.fn();
    render(
      <AssistedPublishingWorkspace
        workspace={{ ...workspace, items: [] }}
        onClose={onClose}
      />,
    );

    expect(screen.getByText("No prepared posts")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close workspace" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("labels the native destination truthfully", () => {
    render(<AssistedPublishingWorkspace workspace={workspace} onClose={vi.fn()} />);

    expect(screen.getByRole("link", { name: "Open channel Posts ↗" })).toHaveAttribute(
      "href",
      workspace.youtube_url,
    );
    expect(screen.queryByText("Open YouTube Studio ↗")).not.toBeInTheDocument();
  });
});
