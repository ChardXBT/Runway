import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Nav } from "./nav";

vi.mock("next/navigation", () => ({
  usePathname: () => "/review",
}));

describe("Nav", () => {
  it("shows the approval and publishing safety surfaces", () => {
    render(<Nav />);
    expect(screen.getByRole("link", { name: "Review" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("Publishing disabled")).toBeInTheDocument();
  });
});
