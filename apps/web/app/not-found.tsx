import { StatePanel } from "@/components/state-panel";

export default function NotFound() {
  return (
    <StatePanel
      variant="empty"
      title="Page not found"
      body="This view does not exist. Check the address, or return to the dashboard and pick another section."
      action={{ href: "/dashboard" }}
      actionLabel="Open Dashboard"
    />
  );
}
