import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Nav } from "./nav";

vi.mock("next/navigation", () => ({
  usePathname: () => "/review",
}));

describe("Nav", () => {
  afterEach(cleanup);

  it("shows the three primary product sections and signed-out status", () => {
    render(<Nav />);
    expect(screen.getByRole("link", { name: "Generator" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Lineup" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connector" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Archive/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Profile/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Activity/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Settings/ })).toBeInTheDocument();
    expect(screen.getByText("Qlob editorial desk")).toBeInTheDocument();
    expect(screen.getByText("Assisted publishing")).toBeInTheDocument();
    expect(screen.getByText("No browser actions are queued")).toBeInTheDocument();
  });

  it("shows the one-post-per-day automation state", () => {
    render(<Nav publishingEnabled publishingMode="authorized_browser" />);
    expect(screen.getByText("Browser publishing ready")).toBeInTheDocument();
    expect(screen.getByText("Actions start from Lineup")).toBeInTheDocument();
  });

  it("closes the backstage menu with Escape and restores focus", async () => {
    render(<Nav />);
    const openMenu = screen.getByLabelText("Open Runway menu");
    fireEvent.click(openMenu);
    const closeMenu = await screen.findByLabelText("Close Runway menu");
    expect(closeMenu.closest(".nav-menu")).toHaveClass("open");

    fireEvent.keyDown(document, { key: "Escape" });

    const reopenedLabel = await screen.findByLabelText("Open Runway menu");
    expect(reopenedLabel.closest(".nav-menu")).not.toHaveClass("open");
    expect(reopenedLabel).toHaveFocus();
  });
});
