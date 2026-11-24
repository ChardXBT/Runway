import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/media-upload", () => ({
  uploadMediaAsset: vi.fn(),
}));

import { uploadMediaAsset } from "@/lib/media-upload";
import { MediaPicker } from "./media-picker";

const uploadMock = vi.mocked(uploadMediaAsset);

describe("MediaPicker", () => {
  beforeEach(() => {
    uploadMock.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("lists the post's existing media", () => {
    render(
      <MediaPicker
        media={[
          { asset_id: 4, kind: "video", status: "ready", label: "clip.mp4" },
        ]}
        onAdded={vi.fn()}
      />,
    );
    expect(screen.getByText("clip.mp4")).toBeInTheDocument();
    expect(screen.getByText("video · ready")).toBeInTheDocument();
  });

  it("uploads a picked file through the resumable flow and reports the asset", async () => {
    uploadMock.mockResolvedValueOnce({
      id: 9,
      kind: "video",
      status: "ready",
    } as Awaited<ReturnType<typeof uploadMediaAsset>>);
    const onAdded = vi.fn();
    render(<MediaPicker media={[]} onAdded={onAdded} />);

    const input = document.querySelector<HTMLInputElement>(
      ".media-picker input[type='file']",
    );
    expect(input).not.toBeNull();
    const file = new File(["bytes"], "clip.mp4", { type: "video/mp4" });
    fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });

    await waitFor(() =>
      expect(onAdded).toHaveBeenCalledWith({
        asset_id: 9,
        kind: "video",
        status: "ready",
        label: "clip.mp4",
      }),
    );
    expect(uploadMock).toHaveBeenCalledWith(
      file,
      expect.objectContaining({ onProgress: expect.any(Function) }),
    );
  });

  it("shows the upload error and keeps the finished parts note", async () => {
    uploadMock.mockRejectedValueOnce(new Error("network gone"));
    render(<MediaPicker media={[]} onAdded={vi.fn()} />);
    const input = document.querySelector<HTMLInputElement>(
      ".media-picker input[type='file']",
    );
    fireEvent.change(input as HTMLInputElement, {
      target: { files: [new File(["x"], "a.mov", { type: "video/quicktime" })] },
    });
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("network gone"),
    );
  });
});
