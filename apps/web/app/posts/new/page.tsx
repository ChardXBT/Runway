import type { Metadata } from "next";

import { PostEditor, editorAccounts } from "@/components/post-editor";
import { apiGetRequired } from "@/lib/api";
import { isPlatformsResponse } from "@/lib/guards";
import { isSettings, type Settings } from "@/lib/settings";
import type { PlatformsResponse } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "New post",
};

export default async function NewPostPage() {
  const [platforms, settings] = await Promise.all([
    apiGetRequired<PlatformsResponse>("/api/platforms", isPlatformsResponse),
    apiGetRequired<Settings>("/api/settings/full", isSettings),
  ]);
  const accounts = platforms.platforms.flatMap((platform) => platform.accounts);
  return (
    <PostEditor
      initialPost={null}
      accounts={editorAccounts(accounts)}
      timezone={settings.timezone}
    />
  );
}
