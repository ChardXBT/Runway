import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TrendChart } from "./trend-chart";

const points = [
  { label: "Jul 1", value: 10 },
  { label: "Jul 2", value: 40 },
  { label: "Jul 3", value: 25 },
];

describe("TrendChart", () => {
  afterEach(cleanup);

  it("renders a line path for n points with a summary aria-label", () => {
    const { container } = render(<TrendChart points={points} label="Views" />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute("role", "img");
    const label = svg?.getAttribute("aria-label") ?? "";
    expect(label).toContain("Views");
    expect(label).toContain("3 points");
    expect(label).toContain("10");
    expect(label).toContain("25");
    expect(container.querySelector(".trend-chart-line")).not.toBeNull();
  });

  it("includes a visually hidden data table for screen readers", () => {
    const { container } = render(<TrendChart points={points} label="Views" />);
    const table = container.querySelector("table");
    expect(table).not.toBeNull();
    expect(table?.className).toContain("visually-hidden");
    expect(table?.querySelectorAll("tbody tr").length).toBe(3);
  });

  it("renders an empty state for no points", () => {
    const { container } = render(<TrendChart points={[]} label="Views" />);
    expect(screen.getByText("No data in range")).toBeInTheDocument();
    expect(container.querySelector("svg")).toBeNull();
  });

  it("renders compact charts without axis labels", () => {
    const { container } = render(
      <TrendChart points={points} label="Views" compact />,
    );
    expect(container.querySelector(".trend-chart-axis")).toBeNull();
  });
});
