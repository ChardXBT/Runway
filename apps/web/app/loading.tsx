export default function Loading() {
  return (
    <section className="route-loading" aria-busy="true" aria-live="polite">
      <span className="route-loading-mark" aria-hidden="true" />
      <div>
        <p className="eyebrow">RunWay</p>
        <h1>Preparing the desk.</h1>
        <p>Loading the latest local state without changing it.</p>
      </div>
    </section>
  );
}
