import { RunwayLogo } from "@/components/runway-logo";

export default function Loading() {
  return (
    <section className="route-loading" aria-busy="true" aria-live="polite">
      <RunwayLogo className="route-loading-mark" />
      <div>
        <p className="eyebrow">Runway</p>
        <h1>Preparing the desk.</h1>
        <p>Loading the latest local state without changing it.</p>
      </div>
    </section>
  );
}
