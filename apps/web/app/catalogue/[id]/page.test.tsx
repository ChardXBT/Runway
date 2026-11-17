import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import CatalogueDetail from "./page";

vi.mock("@/components/annotation-editor", () => ({
  AnnotationEditor: () => null,
}));
vi.mock("@/components/eligibility-toggle", () => ({
  EligibilityToggle: () => null,
}));

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const detail = {
  id: 1,
  external_post_id: "Ugk1",
  permalink: null,
  post_type: "image",
  caption: "Studio morning",
  displayed_date_text: null,
  published_at: null,
  date_precision: "unknown",
  like_count: null,
  comment_count: null,
  is_training_eligible: true,
  media: [],
};

describe("CatalogueDetail", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders similar posts whose caption is null with a placeholder", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).endsWith("/similar")
          ? jsonResponse([
              { post_id: 2, caption: null, score: 0.81 },
              { post_id: 3, caption: "Night shoot", score: 0.5 },
            ])
          : jsonResponse(detail),
      ),
    );

    render(await CatalogueDetail({ params: Promise.resolve({ id: "1" }) }));

    const placeholder = screen.getByText("Caption unavailable");
    expect(placeholder.closest("a")).toHaveAttribute("href", "/catalogue/2");
    expect(screen.getByText("81%")).toBeInTheDocument();
    expect(screen.getByText("Night shoot")).toBeInTheDocument();
  });
});
