import Link from "next/link";

import { API_URL, apiGet } from "@/lib/api";

type Media = { id: number; url: string; width: number; height: number };
type Post = {
  id: number;
  post_type: string;
  caption: string | null;
  published_at: string | null;
  date_precision: string;
  is_training_eligible: boolean;
  media: Media[];
};

export default async function CataloguePage({
  searchParams,
}: {
  searchParams: Promise<{ search?: string; type?: string; eligible?: string; franchise?: string; character?: string }>;
}) {
  const params = await searchParams;
  const query = new URLSearchParams({ limit: "100" });
  if (params.search) query.set("search", params.search);
  if (params.type) query.set("post_type", params.type);
  if (params.eligible) query.set("training_eligible", params.eligible);
  if (params.franchise) query.set("franchise", params.franchise);
  if (params.character) query.set("character", params.character);
  const posts = await apiGet<Post[]>(`/api/catalog?${query}`, []);

  return (
    <>
      <header className="header-row">
        <div>
          <p className="eyebrow">Historical catalogue</p>
          <h1>What Qlob has already said.</h1>
          <p className="lede">Raw provenance stays intact; eligibility and date precision stay visible.</p>
        </div>
      </header>
      <form className="filter-bar" method="get">
        <label>
          <span>Search captions</span>
          <input name="search" defaultValue={params.search} placeholder="dramatic entrance" />
        </label>
        <label><span>Franchise / show</span><input name="franchise" defaultValue={params.franchise} placeholder="Synthetic Comedy" /></label>
        <label><span>Character</span><input name="character" defaultValue={params.character} placeholder="Fixture character" /></label>
        <label>
          <span>Post type</span>
          <select name="type" defaultValue={params.type ?? ""}>
            <option value="">All types</option>
            <option value="image">Image</option>
            <option value="multi_image">Multi-image</option>
            <option value="text">Text</option>
            <option value="poll">Poll</option>
            <option value="video_share">Video share</option>
          </select>
        </label>
        <label>
          <span>Profile use</span>
          <select name="eligible" defaultValue={params.eligible ?? ""}>
            <option value="">All records</option>
            <option value="true">Training eligible</option>
            <option value="false">Excluded</option>
          </select>
        </label>
        <button className="button" type="submit">Filter</button>
      </form>
      {posts.length ? (
        <section className="catalog-grid" aria-label="Historical posts">
          {posts.map((post) => (
            <Link href={`/catalogue/${post.id}`} className="catalog-card" key={post.id}>
              <div className="catalog-image">
                {post.media[0] ? (
                  // Local API media; originals are not optimized or transformed by Next.js.
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={`${API_URL}${post.media[0].url}`} alt="Historical post media" />
                ) : (
                  <span>No image</span>
                )}
              </div>
              <div className="catalog-copy">
                <div className="meta-row">
                  <span>{post.post_type.replace("_", " ")}</span>
                  <span>{post.date_precision}</span>
                  {post.is_training_eligible && <span className="pill">Profile</span>}
                </div>
                <h2>{post.caption || "Caption unavailable"}</h2>
                <small>{post.published_at ? new Date(post.published_at).toLocaleDateString() : "Source date unresolved"}</small>
              </div>
            </Link>
          ))}
        </section>
      ) : (
        <section className="panel empty">
          <div><strong>No catalogue records yet.</strong>Run the offline fixture capture or an explicit headed capture.</div>
        </section>
      )}
    </>
  );
}
