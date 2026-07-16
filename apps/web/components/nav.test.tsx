import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Nav } from "./nav";

describe("Nav", () => {
  it("shows the approval and publishing safety surfaces", () => {
    render(<Nav />);
    expect(screen.getByRole("link", { name: "Review" })).toBeInTheDocument();
    expect(screen.getByText("Publishing disabled")).toBeInTheDocument();
  });
});
