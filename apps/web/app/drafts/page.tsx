import type { Metadata } from "next";

import { DraftsWorkspace } from "@/components/drafts-workspace";
import { apiGetRequired } from "@/lib/api";
import { isDraftList, isPlatformsResponse } from "@/lib/guards";
import { isSettings, type Settings } from "@/lib/settings";
import type { DraftView, PlatformsResponse } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Drafts",
};

export default async function DraftsPage() {
  const [platforms, drafts, settings] = await Promise.all([
    apiGetRequired<PlatformsResponse>("/api/platforms", isPlatformsResponse),
    apiGetRequired<DraftView[]>("/api/drafts", isDraftList),
    apiGetRequired<Settings>("/api/settings/full", isSettings),
  ]);
  return (
    <DraftsWorkspace
      platforms={platforms}
      drafts={drafts}
      timezone={settings.timezone}
    />
  );
}
