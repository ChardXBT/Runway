import Link from "next/link";

import { API_URL, apiGet } from "@/lib/api";
import { AnnotationEditor } from "@/components/annotation-editor";
import { EligibilityToggle } from "@/components/eligibility-toggle";
import { isRecord } from "@/lib/guards";

type Detail = {
  id: number;
  external_post_id: string | null;
  permalink: string | null;
  post_type: string;
  caption: string | null;
  displayed_date_text: string | null;
  published_at: string | null;
  date_precision: string;
  like_count: number | null;
  comment_count: number | null;
  is_training_eligible: boolean;
  media: { id: number; url: string; width: number; height: number; sha256: string }[];
};

type Similar = { post_id: number; caption: string; score: number };

function isDetail(value: unknown): value is Detail {
  return (
    isRecord(value) &&
    typeof value.id === "number" &&
    (value.external_post_id === null ||
      typeof value.external_post_id === "string") &&
    (value.permalink === null || typeof value.permalink === "string") &&
    typeof value.post_type === "string" &&
    (value.caption === null || typeof value.caption === "string") &&
    (value.displayed_date_text === null ||
      typeof value.displayed_date_text === "string") &&
    (value.published_at === null || typeof value.published_at === "string") &&
    typeof value.date_precision === "string" &&
    (value.like_count === null || typeof value.like_count === "number") &&
    (value.comment_count === null || typeof value.comment_count === "number") &&
    typeof value.is_training_eligible === "boolean" &&
    Array.isArray(value.media) &&
    value.media.every(
      (media) =>
        isRecord(media) &&
        typeof media.id === "number" &&
        typeof media.url === "string" &&
        typeof media.width === "number" &&
        typeof media.height === "number" &&
        typeof media.sha256 === "string",
    )
  );
}

function isSimilarList(value: unknown): value is Similar[] {
  return (
    Array.isArray(value) &&
    value.every(
      (item) =>
        isRecord(item) &&
        typeof item.post_id === "number" &&
        typeof item.caption === "string" &&
        typeof item.score === "number",
    )
  );
}

function splitCaption(caption: string | null) {
  if (!caption) return { text: "Caption unavailable", url: null };
  const match = caption.match(/https?:\/\/\S+/);
  if (!match) return { text: caption, url: null };
  const url = match[0];
  const text = caption.replace(url, "").trim().replace(/[:\-–—]\s*$/, "");
  return { text: text || "Linked post", url };
}

export default async function CatalogueDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const post = await apiGet<Detail | null>(
    `/api/catalog/${id}`,
    null,
    (value): value is Detail | null => value === null || isDetail(value),
  );
  const similar = await apiGet<Similar[]>(
    `/api/catalog/${id}/similar`,
    [],
    isSimilarList,
  );
  if (!post) {
    return <section className="panel empty"><div><strong>Post not found.</strong><Link href="/catalogue">Back to catalogue</Link></div></section>;
  }
  const caption = splitCaption(post.caption);
  return (
    <>
      <Link className="back-link" href="/catalogue">← Catalogue</Link>
      <header className="detail-heading">
        <p className="eyebrow">Historical post #{post.id}</p>
        <h1>{caption.text}</h1>
        {caption.url && (
          <a className="caption-url" href={caption.url} target="_blank" rel="noreferrer">
            {caption.url}
          </a>
        )}
      </header>
      <section className="detail-grid">
        <div className="detail-media">
          {post.media.map((media) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img key={media.id} src={`${API_URL}${media.url}`} alt="Historical post original" />
          ))}
        </div>
        <aside className="panel record-facts">
          <h2>Source record</h2>
          <dl>
            <dt>Type</dt><dd>{post.post_type}</dd>
            <dt>Displayed date</dt><dd>{post.displayed_date_text || "Unavailable"}</dd>
            <dt>Date precision</dt><dd>{post.date_precision}</dd>
            <dt>Likes</dt><dd>{post.like_count ?? "Hidden"}</dd>
            <dt>Comments</dt><dd>{post.comment_count ?? "Hidden"}</dd>
            <dt>Profile eligible</dt><dd>{post.is_training_eligible ? "Yes" : "No"}</dd>
            <dt>External ID</dt><dd>{post.external_post_id || "Unavailable"}</dd>
          </dl>
          {post.permalink && <a className="button secondary" href={post.permalink} target="_blank" rel="noreferrer">Open source post</a>}
          <EligibilityToggle postId={post.id} initial={post.is_training_eligible} />
        </aside>
      </section>
      <AnnotationEditor postId={post.id} />
      <section className="panel"><p className="eyebrow">Closest local matches</p><div className="quote-list">{similar.length ? similar.map((match) => <Link href={`/catalogue/${match.post_id}`} key={match.post_id}><span>{Math.round(match.score * 100)}%</span><q>{match.caption}</q></Link>) : <p>Build similarity edges by running historical analysis.</p>}</div></section>
    </>
  );
}
