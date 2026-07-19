"use client";

export default function ErrorPage({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <section className="route-error" role="alert">
      <p className="eyebrow danger">Interface unavailable</p>
      <h1>Runway could not open this view.</h1>
      <p>
        Nothing was submitted. Check that the local service is running, then try
        loading the page again.
      </p>
      <button className="button secondary" type="button" onClick={reset}>
        Try this view again
      </button>
    </section>
  );
}
