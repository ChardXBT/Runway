"use client";

export default function ErrorPage({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <section className="route-error" role="alert">
      <p className="eyebrow danger">View unavailable</p>
      <h1>This section did not load.</h1>
      <p>
        This view could not be loaded. Check the local service, then verify Generator,
        Lineup, or Activity before repeating the previous action.
      </p>
      <button className="button secondary" type="button" onClick={reset}>
        Try this view again
      </button>
    </section>
  );
}
