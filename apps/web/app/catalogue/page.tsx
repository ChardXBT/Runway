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

type CatalogStatus = {
  total_posts: number;
  training_eligible: number;
  media_assets: number;
};

export default async function CataloguePage({
  searchParams,
}: {
  searchParams: Promise<{
    search?: string;
    type?: string;
    eligible?: string;
    franchise?: string;
    character?: string;
    page?: string;
  }>;
}) {
  const params = await searchParams;
  const pageSize = 60;
  const page = Math.max(1, Number.parseInt(params.page ?? "1", 10) || 1);
  const query = new URLSearchParams({
    limit: String(pageSize + 1),
    offset: String((page - 1) * pageSize),
  });
  if (params.search) query.set("search", params.search);
  if (params.type) query.set("post_type", params.type);
  if (params.eligible) query.set("training_eligible", params.eligible);
  if (params.franchise) query.set("franchise", params.franchise);
  if (params.character) query.set("character", params.character);
  const [catalogRows, status] = await Promise.all([
    apiGet<Post[]>(`/api/catalog?${query}`, []),
    apiGet<CatalogStatus>("/api/catalog/status", {
      total_posts: 0,
      training_eligible: 0,
      media_assets: 0,
    }),
  ]);
  const posts = catalogRows.slice(0, pageSize);
  const hasNextPage = catalogRows.length > pageSize;
  const makePageHref = (target: number) => {
    const next = new URLSearchParams();
    if (params.search) next.set("search", params.search);
    if (params.type) next.set("type", params.type);
    if (params.eligible) next.set("eligible", params.eligible);
    if (params.franchise) next.set("franchise", params.franchise);
    if (params.character) next.set("character", params.character);
    if (target > 1) next.set("page", String(target));
    const encoded = next.toString();
    return encoded ? `/catalogue?${encoded}` : "/catalogue";
  };

  return (
    <>
      <header className="page-header">
        <div>
          <p className="eyebrow">Archive / page {page}</p>
          <h1>Qlob’s visual memory.</h1>
          <p className="lede">
            Search every captured caption and inspect the image, source record, model annotation,
            and closest historical matches.
          </p>
        </div>
        <div className="header-counter">
          <strong>{status.total_posts}</strong>
          <span>{posts.length} shown on page {page}</span>
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
        <div className="filter-actions">
          <button className="button" type="submit">Apply filters</button>
          <Link className="text-link" href="/catalogue">Reset</Link>
        </div>
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
                  <span className="record-id">#{post.id}</span>
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
          <div>
            <strong>No records match these filters.</strong>
            Reset the filters or return to an earlier page.
          </div>
        </section>
      )}
      <nav className="pagination" aria-label="Archive pages">
        {page > 1 ? <Link className="button secondary" href={makePageHref(page - 1)}>Previous</Link> : <span />}
        <span>Page {page}</span>
        {hasNextPage ? <Link className="button secondary" href={makePageHref(page + 1)}>Next</Link> : <span />}
      </nav>
    </>
  );
}
