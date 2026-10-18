import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MetricTile } from "./metric-tile";

describe("MetricTile", () => {
  afterEach(cleanup);

  it("formats the value and a positive delta tone", () => {
    const { container } = render(
      <MetricTile label="Views" value={1200} delta={340} source="YouTube Analytics API" />,
    );
    expect(screen.getByText("Views")).toBeInTheDocument();
    expect(screen.getByText("1.2K")).toBeInTheDocument();
    const delta = container.querySelector(".metric-tile-delta");
    expect(delta).toHaveAttribute("data-tone", "positive");
    expect(delta?.textContent).toBe("+340");
    expect(screen.getByText(/YouTube Analytics API/)).toBeInTheDocument();
  });

  it("marks negative and neutral deltas", () => {
    const { container } = render(<MetricTile label="Followers" value={10} delta={-5} />);
    expect(container.querySelector(".metric-tile-delta")).toHaveAttribute(
      "data-tone",
      "negative",
    );

    cleanup();
    const second = render(
      <MetricTile label="Reach" value={10} delta={0} deltaTone="auto" />,
    );
    expect(second.container.querySelector(".metric-tile-delta")).toHaveAttribute(
      "data-tone",
      "neutral",
    );
  });

  it("renders a placeholder for missing values", () => {
    render(<MetricTile label="Comments" value={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
