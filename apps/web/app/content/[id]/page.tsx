import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { ContentDetailView } from "@/components/content-detail";
import { apiGetOptional } from "@/lib/api";
import { isContentDetail } from "@/lib/guards";
import type { ContentDetail } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Content",
};

export default async function ContentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const detail = await apiGetOptional<ContentDetail>(
    `/api/content/${id}`,
    isContentDetail,
  );
  if (detail === null) notFound();
  return <ContentDetailView detail={detail} />;
}
