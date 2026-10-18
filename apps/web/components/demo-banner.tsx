export function DemoBanner() {
  return (
    <div className="demo-banner" role="status">
      <span>
        Demo providers active — fictional accounts and numbers. Set{" "}
        <code>RUNWAY_PLATFORM_PROVIDER=live</code> to use real accounts.
      </span>
    </div>
  );
}
