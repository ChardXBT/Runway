/* eslint-disable @next/next/no-img-element */
import Link from "next/link";

import { apiGet } from "@/lib/api";

type Profile = {
  version: number;
  summary: string;
  training_post_ids: number[];
  holdout_post_ids: number[];
  caption_statistics: {
    median_words: number;
    question_frequency: number;
    exclamation_frequency: number;
  };
  franchise_distribution: [string, number][];
  character_distribution: [string, number][];
  visual_compositions: [string, number][];
  representative_positive_examples: { post_id: number; caption: string; media_url?: string | null }[];
  rotation_patterns: string[];
};

type Evaluation = {
  profile_version: number;
  image_caption_matching: { accuracy: number; evaluated: number };
  qlob_caption_ranking_accuracy: number;
  retrieval_top3_franchise_relevance: number;
  duplicate_detection: { transformed_true_positive_rate: number; unrelated_false_positive_rate: number };
};

function Percent({ value }: { value: number }) {
  return <>{new Intl.NumberFormat("en", { style: "percent", maximumFractionDigits: 1 }).format(value)}</>;
}

export default async function ProfilePage() {
  const profile = await apiGet<Profile | null>("/api/profiles/active", null);
  const evaluation = await apiGet<Evaluation | null>("/api/profiles/evaluation", null);
  if (!profile) {
    return <><p className="eyebrow">Style intelligence</p><h1>No profile yet.</h1><section className="panel empty"><div><strong>Analyze the fixture catalogue first.</strong>Run <code>leeway analyze history --resume</code>, then <code>leeway profile build</code>.</div></section></>;
  }
  return (
    <>
      <header className="header-row">
        <div><p className="eyebrow">Qlob style profile · v{profile.version}</p><h1>Patterns, with receipts.</h1><p className="lede">A reproducible retrieval profile built from local statistics and cited examples—not model-weight fine-tuning.</p></div>
      </header>
      <section className="profile-hero panel"><p>{profile.summary}</p></section>
      <section className="stat-grid profile-stats" aria-label="Profile statistics">
        <article className="stat"><span>Training records</span><strong>{profile.training_post_ids.length}</strong></article>
        <article className="stat"><span>Holdout records</span><strong>{profile.holdout_post_ids.length}</strong></article>
        <article className="stat"><span>Median words</span><strong>{profile.caption_statistics.median_words}</strong></article>
        <article className="stat"><span>Questions</span><strong><Percent value={profile.caption_statistics.question_frequency} /></strong></article>
      </section>
      <section className="profile-columns">
        <article className="panel distribution"><p className="eyebrow">Topic rotation</p><h2>Franchises</h2>{profile.franchise_distribution.map(([name, count]) => <div key={name}><span>{name}</span><strong>{count}</strong></div>)}</article>
        <article className="panel distribution"><p className="eyebrow">Visual language</p><h2>Compositions</h2>{profile.visual_compositions.map(([name, count]) => <div key={name}><span>{name}</span><strong>{count}</strong></div>)}</article>
      </section>
      <section className="panel"><p className="eyebrow">Representative images + captions</p><div className="quote-list">{profile.representative_positive_examples.map((example) => <Link href={`/catalogue/${example.post_id}`} key={example.post_id}>{example.media_url && <img src={`${process.env.NEXT_PUBLIC_LEEWAY_API_URL ?? "http://127.0.0.1:8000"}${example.media_url}`} alt="" />}<span>#{example.post_id}</span><q>{example.caption}</q></Link>)}</div></section>
      <section className="panel"><p className="eyebrow">Rotation observations</p><ul className="clean-list">{profile.rotation_patterns.map((item) => <li key={item}>{item}</li>)}</ul></section>
      {evaluation && <section className="panel"><p className="eyebrow">Measured holdout evaluation · v{evaluation.profile_version}</p><div className="evaluation-grid"><div><span>Image-caption matching</span><strong><Percent value={evaluation.image_caption_matching.accuracy} /></strong></div><div><span>Caption ranking</span><strong><Percent value={evaluation.qlob_caption_ranking_accuracy} /></strong></div><div><span>Retrieval relevance</span><strong><Percent value={evaluation.retrieval_top3_franchise_relevance} /></strong></div><div><span>Duplicate recall</span><strong><Percent value={evaluation.duplicate_detection.transformed_true_positive_rate} /></strong></div></div><small>Synthetic fixture metrics are directional; they do not establish real-channel quality.</small></section>}
    </>
  );
}
