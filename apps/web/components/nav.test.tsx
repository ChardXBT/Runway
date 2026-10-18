import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Nav } from "./nav";

let mockedPathname = "/review";

vi.mock("next/navigation", () => ({
  usePathname: () => mockedPathname,
}));

describe("Nav", () => {
  afterEach(() => {
    cleanup();
    mockedPathname = "/review";
  });

  it("renders the grouped navigation sections", { timeout: 15000 }, () => {
    render(<Nav />);
    expect(
      screen.getByText("Overview", { selector: ".nav-section-label" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Create and schedule")).toBeInTheDocument();
    expect(screen.getByText("Audience")).toBeInTheDocument();
    expect(screen.getByText("Workspace")).toBeInTheDocument();
    for (const label of [
      "Dashboard",
      "Review",
      "Calendar",
      "Drafts",
      "Analytics",
      "Inbox",
      "Content",
      "Accounts",
      "Connector",
      "Archive",
      "Editorial voice",
      "Activity",
      "Settings",
    ]) {
      expect(
        screen.getByRole("link", { name: new RegExp(label) }),
      ).toBeInTheDocument();
    }
    expect(screen.getByText("Creator desk")).toBeInTheDocument();
    expect(screen.getByText("Captured Community history")).toBeInTheDocument();
    expect(screen.getByText("Assisted publishing")).toBeInTheDocument();
    expect(
      screen.getByText("No browser actions are queued"),
    ).toBeInTheDocument();
  });

  it("marks the active link and honours aliases", () => {
    render(<Nav />);
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    cleanup();
    mockedPathname = "/queue";
    render(<Nav />);
    expect(screen.getByRole("link", { name: "Calendar" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    cleanup();
    mockedPathname = "/catalogue";
    render(<Nav />);
    expect(screen.getByRole("link", { name: /Archive/ })).toHaveAttribute(
      "aria-current",
      "page",
    );

    cleanup();
    mockedPathname = "/accounts/3";
    render(<Nav />);
    expect(screen.getByRole("link", { name: "Accounts" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("shows the one-post-per-day automation state", () => {
    render(<Nav publishingEnabled publishingMode="authorized_browser" />);
    expect(screen.getByText("Browser publishing ready")).toBeInTheDocument();
    expect(screen.getByText("Actions start from Calendar")).toBeInTheDocument();
  });

  it("closes the menu with Escape and restores focus", async () => {
    render(<Nav />);
    const trigger = screen.getByLabelText("Open Runway menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveAttribute("aria-controls", "runway-nav-drawer");
    fireEvent.click(trigger);
    const closeTrigger = await screen.findByLabelText("Close Runway menu");
    expect(closeTrigger).toHaveAttribute("aria-expanded", "true");
    expect(closeTrigger.closest(".nav-menu")).toHaveClass("open");

    fireEvent.keyDown(document, { key: "Escape" });

    const reopened = await screen.findByLabelText("Open Runway menu");
    expect(reopened.closest(".nav-menu")).not.toHaveClass("open");
    expect(reopened).toHaveFocus();
  });
});
