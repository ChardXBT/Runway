import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { PostEditor, editorAccounts } from "@/components/post-editor";
import { apiGetOptional, apiGetRequired } from "@/lib/api";
import { isPlatformsResponse } from "@/lib/guards";
import { isSettings, type Settings } from "@/lib/settings";
import type { PlatformsResponse } from "@/lib/types";
import { isPostOut, type PostOut } from "@/lib/v1-guards";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Post",
};

export default async function PostPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const postId = Number.parseInt(id, 10);
  if (!Number.isInteger(postId) || postId <= 0) notFound();
  const [post, platforms, settings] = await Promise.all([
    apiGetOptional<PostOut>(`/api/v1/posts/${postId}`, isPostOut),
    apiGetRequired<PlatformsResponse>("/api/platforms", isPlatformsResponse),
    apiGetRequired<Settings>("/api/settings/full", isSettings),
  ]);
  if (post === null) notFound();
  const accounts = platforms.platforms.flatMap((platform) => platform.accounts);
  return (
    <PostEditor
      initialPost={post}
      accounts={editorAccounts(accounts)}
      timezone={settings.timezone}
    />
  );
}
