import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { StatePanel } from "./state-panel";

describe("StatePanel", () => {
  afterEach(cleanup);

  it("renders the loading skeleton", () => {
    const { container } = render(<StatePanel variant="loading" />);
    expect(
      container.querySelectorAll(".state-panel-skeleton").length,
    ).toBeGreaterThan(0);
    expect(
      container.querySelector(".state-panel"),
    ).toHaveAttribute("aria-busy", "true");
  });

  it("renders variant title, body, and link action", () => {
    render(
      <StatePanel
        variant="not_configured"
        body="Add credentials to .env"
        action={{ href: "/accounts" }}
        actionLabel="Open Accounts"
      />,
    );
    expect(screen.getByText("Platform not configured")).toBeInTheDocument();
    expect(screen.getByText("Add credentials to .env")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open Accounts" }),
    ).toHaveAttribute("href", "/accounts");
  });

  it("supports a custom title and button action", () => {
    let clicked = 0;
    render(
      <StatePanel
        variant="empty"
        title="Nothing here"
        action={{ onClick: () => (clicked += 1) }}
        actionLabel="Retry"
      />,
    );
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
    screen.getByRole("button", { name: "Retry" }).click();
    expect(clicked).toBe(1);
  });
});
