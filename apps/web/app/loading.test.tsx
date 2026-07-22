import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import Loading from "./loading";

describe("route loading state", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("shows immediate progress and explains a longer wait", () => {
    vi.useFakeTimers();
    render(<Loading />);

    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("Opening the latest local state.")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(5_000));

    expect(screen.getByText("Still working.")).toBeInTheDocument();
    expect(
      screen.getByText(/The local service is taking longer than usual/i),
    ).toBeInTheDocument();
  });
});
