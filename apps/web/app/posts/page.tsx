import type { Metadata } from "next";

import { PostsWorkspace } from "@/components/posts-workspace";
import { apiGetRequired } from "@/lib/api";
import { isPostPage, type PostPage } from "@/lib/v1-guards";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Posts",
};

export default async function PostsPage() {
  const page = await apiGetRequired<PostPage>("/api/v1/posts", isPostPage);
  return <PostsWorkspace initialPage={page} />;
}
