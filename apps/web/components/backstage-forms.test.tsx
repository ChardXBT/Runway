import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnnotationEditor } from "./annotation-editor";
import { EligibilityToggle } from "./eligibility-toggle";

const annotation = {
  effective: {
    franchise: "The Simpsons",
    visible_characters: ["Homer"],
    scene_description: "Homer is smiling.",
    composition: "close-up",
    tone: "excited",
  },
  review_status: "model",
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("backstage forms", () => {
  it("locks duplicate annotation saves and confirms only validated data", async () => {
    let resolveSave!: (value: unknown) => void;
    const pendingSave = new Promise((resolve) => {
      resolveSave = resolve;
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => annotation,
      })
      .mockReturnValueOnce(pendingSave);
    vi.stubGlobal("fetch", fetchMock);
    render(<AnnotationEditor postId={8} />);

    const scene = await screen.findByLabelText("Scene description");
    fireEvent.change(scene, { target: { value: "Wait... what?!" } });
    const form = screen
      .getByRole("button", { name: "Save review" })
      .closest("form");
    fireEvent.submit(form!);
    fireEvent.submit(form!);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(
      screen.getByRole("button", { name: "Saving review…" }),
    ).toBeDisabled();

    resolveSave({
      ok: true,
      json: async () => ({
        ...annotation,
        review_status: "reviewed",
        effective: {
          ...annotation.effective,
          scene_description: "Wait... what?!",
        },
      }),
    });

    expect(await screen.findByText(/Review saved/)).toBeInTheDocument();
  });

  it("keeps annotation input after a network error", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => annotation,
      })
      .mockRejectedValueOnce(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);
    render(<AnnotationEditor postId={9} />);

    const tone = await screen.findByLabelText("Tone");
    fireEvent.change(tone, { target: { value: "confused?!" } });
    fireEvent.click(screen.getByRole("button", { name: "Save review" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "response could not be verified",
    );
    expect(screen.getByLabelText("Tone")).toHaveValue("confused?!");
  });

  it("does not flip eligibility when the response is malformed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ is_training_eligible: "maybe" }),
      }),
    );
    render(<EligibilityToggle postId={10} initial />);

    fireEvent.click(
      screen.getByRole("button", { name: "Exclude from profile" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "response could not be verified",
    );
    expect(
      screen.getByRole("button", { name: "Exclude from profile" }),
    ).toBeInTheDocument();
  });

  it("submits an eligibility toggle only once while it is running", async () => {
    let resolveSave!: (value: unknown) => void;
    const pendingSave = new Promise((resolve) => {
      resolveSave = resolve;
    });
    const fetchMock = vi.fn().mockReturnValue(pendingSave);
    vi.stubGlobal("fetch", fetchMock);
    render(<EligibilityToggle postId={11} initial={false} />);

    const button = screen.getByRole("button", { name: "Include in profile" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveSave({
      ok: true,
      json: async () => ({ is_training_eligible: true }),
    });

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Exclude from profile" }),
      ).toBeEnabled(),
    );
    expect(screen.getByText("Eligibility saved.")).toBeInTheDocument();
  });

  it("reconciles an ambiguous annotation save without losing the draft", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => annotation })
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce({ ok: true, json: async () => annotation });
    vi.stubGlobal("fetch", fetchMock);
    render(<AnnotationEditor postId={12} />);

    const scene = await screen.findByLabelText("Scene description");
    fireEvent.change(scene, { target: { value: "A preserved correction." } });
    fireEvent.click(screen.getByRole("button", { name: "Save review" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "response could not be verified",
    );
    expect(screen.getByRole("button", { name: "Save review" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Check saved state" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "save did not take effect",
    );
    expect(scene).toHaveValue("A preserved correction.");
    expect(screen.getByRole("button", { name: "Save review" })).toBeEnabled();
  });

  it("reconciles eligibility after an ambiguous response", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ is_training_eligible: "maybe" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ is_training_eligible: false }),
      });
    vi.stubGlobal("fetch", fetchMock);
    render(<EligibilityToggle postId={13} initial />);

    fireEvent.click(screen.getByRole("button", { name: "Exclude from profile" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "response could not be verified",
    );
    expect(
      screen.getByRole("button", { name: "Exclude from profile" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Check saved state" }));

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Include in profile" }),
      ).toBeEnabled(),
    );
    expect(screen.getByText("Eligibility save verified.")).toBeInTheDocument();
  });
});
