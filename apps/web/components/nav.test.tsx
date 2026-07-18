import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Nav } from "./nav";

vi.mock("next/navigation", () => ({
  usePathname: () => "/review",
}));

describe("Nav", () => {
  afterEach(cleanup);

  it("shows the two primary product sections and signed-out status", () => {
    render(<Nav />);
    expect(screen.getByRole("link", { name: "Generator" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Lineup" })).toBeInTheDocument();
    expect(screen.getByText("YouTube scheduling off")).toBeInTheDocument();
  });

  it("shows the one-post-per-day automation state", () => {
    render(<Nav publishingEnabled />);
    expect(screen.getByText("YouTube actions enabled")).toBeInTheDocument();
    expect(screen.getByText("Account sign-in is checked on use")).toBeInTheDocument();
  });
});
