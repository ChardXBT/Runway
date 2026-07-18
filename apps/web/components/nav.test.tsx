import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

  it("closes the backstage menu with Escape and restores focus", async () => {
    render(<Nav />);
    const openMenu = screen.getByLabelText("Open RunWay menu");
    fireEvent.click(openMenu);
    const closeMenu = await screen.findByLabelText("Close RunWay menu");
    expect(closeMenu.closest("details")).toHaveAttribute("open");

    fireEvent.keyDown(document, { key: "Escape" });

    const reopenedLabel = await screen.findByLabelText("Open RunWay menu");
    expect(reopenedLabel.closest("details")).not.toHaveAttribute("open");
    expect(reopenedLabel).toHaveFocus();
  });
});
