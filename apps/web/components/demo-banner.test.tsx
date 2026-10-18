import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DemoBanner } from "./demo-banner";

describe("DemoBanner", () => {
  afterEach(cleanup);

  it("explains that fictional data is active", () => {
    render(<DemoBanner />);
    expect(
      screen.getByText(/Demo providers active — fictional accounts/),
    ).toBeInTheDocument();
    expect(screen.getByText(/RUNWAY_PLATFORM_PROVIDER=live/)).toBeInTheDocument();
  });
});
