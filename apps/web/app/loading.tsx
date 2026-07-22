"use client";

import { useEffect, useState } from "react";

export default function Loading() {
  const [takingLonger, setTakingLonger] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setTakingLonger(true), 5000);
    return () => window.clearTimeout(timer);
  }, []);

  return (
    <section
      className="route-loading"
      role="status"
      aria-busy="true"
      aria-live="polite"
    >
      <div className="route-loading-heading">
        <span className="route-kicker">Loading workspace</span>
        <h1>{takingLonger ? "Still working." : "Opening the latest local state."}</h1>
        <p>
          {takingLonger
            ? "The local service is taking longer than usual. You can open another section and return without interrupting it."
            : "Runway is checking the local service and preparing this view."}
        </p>
      </div>
      <span className="route-progress" aria-hidden="true" />
      <div className="route-skeleton" aria-hidden="true">
        <span className="route-skeleton-wide" />
        <span />
        <span />
        <span className="route-skeleton-tall" />
      </div>
    </section>
  );
}
