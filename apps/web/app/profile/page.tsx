/* eslint-disable @next/next/no-img-element */
import Link from "next/link";

import { apiGetRequired } from "@/lib/api";
import { isRecord } from "@/lib/guards";

export const dynamic = "force-dynamic";

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

type IntelligenceStatus = {
  feedback_signals: {
    caption: number;
    image: number;
    pairing: number;
    total: number;
  };
  human_pairwise_labels: number;
  human_pairwise_labels_by_target: Record<"caption" | "image" | "pairing", number>;
  training_pairwise_labels_by_target: Record<
    "caption" | "image" | "pairing",
    number
  >;
  preference_datasets: number;
  active_models: { id: number; target: string; label_count: number }[];
  active_model_targets: string[];
  training_minimum_labels_by_target: Record<
    "caption" | "image" | "pairing",
    number
  >;
  training_minimum_labels_per_target: number;
  trainable_targets: string[];
  blind_study_responses: number;
  blind_study_target: number;
  state: "collecting_creator_labels" | "ready_to_train" | "active";
};

const intelligenceTargets = ["caption", "image", "pairing"] as const;

function isTargetCounts(
  value: unknown,
): value is Record<(typeof intelligenceTargets)[number], number> {
  return (
    isRecord(value) &&
    intelligenceTargets.every((target) => typeof value[target] === "number")
  );
}

function isProfile(value: unknown): value is Profile {
  const isDistribution = (distribution: unknown) =>
    Array.isArray(distribution) &&
    distribution.every(
      (item) =>
        Array.isArray(item) &&
        item.length === 2 &&
        typeof item[0] === "string" &&
        typeof item[1] === "number",
    );
  return (
    isRecord(value) &&
    typeof value.version === "number" &&
    typeof value.summary === "string" &&
    Array.isArray(value.training_post_ids) &&
    Array.isArray(value.holdout_post_ids) &&
    isRecord(value.caption_statistics) &&
    typeof value.caption_statistics.median_words === "number" &&
    typeof value.caption_statistics.question_frequency === "number" &&
    typeof value.caption_statistics.exclamation_frequency === "number" &&
    isDistribution(value.franchise_distribution) &&
    isDistribution(value.character_distribution) &&
    isDistribution(value.visual_compositions) &&
    Array.isArray(value.representative_positive_examples) &&
    value.representative_positive_examples.every(
      (example) =>
        isRecord(example) &&
        typeof example.post_id === "number" &&
        typeof example.caption === "string" &&
        (example.media_url === undefined ||
          example.media_url === null ||
          typeof example.media_url === "string"),
    ) &&
    Array.isArray(value.rotation_patterns) &&
    value.rotation_patterns.every((item) => typeof item === "string")
  );
}

function isEvaluation(value: unknown): value is Evaluation {
  return (
    isRecord(value) &&
    typeof value.profile_version === "number" &&
    isRecord(value.image_caption_matching) &&
    typeof value.image_caption_matching.accuracy === "number" &&
    typeof value.image_caption_matching.evaluated === "number" &&
    typeof value.qlob_caption_ranking_accuracy === "number" &&
    typeof value.retrieval_top3_franchise_relevance === "number" &&
    isRecord(value.duplicate_detection) &&
    typeof value.duplicate_detection.transformed_true_positive_rate ===
      "number" &&
    typeof value.duplicate_detection.unrelated_false_positive_rate === "number"
  );
}

function isIntelligenceStatus(value: unknown): value is IntelligenceStatus {
  return (
    isRecord(value) &&
    isRecord(value.feedback_signals) &&
    typeof value.feedback_signals.caption === "number" &&
    typeof value.feedback_signals.image === "number" &&
    typeof value.feedback_signals.pairing === "number" &&
    typeof value.feedback_signals.total === "number" &&
    typeof value.human_pairwise_labels === "number" &&
    isTargetCounts(value.human_pairwise_labels_by_target) &&
    isTargetCounts(value.training_pairwise_labels_by_target) &&
    typeof value.preference_datasets === "number" &&
    Array.isArray(value.active_models) &&
    value.active_models.every(
      (model) =>
        isRecord(model) &&
        typeof model.id === "number" &&
        typeof model.target === "string" &&
        typeof model.label_count === "number",
    ) &&
    Array.isArray(value.active_model_targets) &&
    value.active_model_targets.every((target) => typeof target === "string") &&
    isTargetCounts(value.training_minimum_labels_by_target) &&
    typeof value.training_minimum_labels_per_target === "number" &&
    Array.isArray(value.trainable_targets) &&
    value.trainable_targets.every((target) => typeof target === "string") &&
    typeof value.blind_study_responses === "number" &&
    typeof value.blind_study_target === "number" &&
    ["collecting_creator_labels", "ready_to_train", "active"].includes(
      String(value.state),
    )
  );
}

function Percent({ value }: { value: number }) {
  return <>{new Intl.NumberFormat("en", { style: "percent", maximumFractionDigits: 1 }).format(value)}</>;
}

export default async function ProfilePage() {
  const [profile, evaluation, intelligence] = await Promise.all([
    apiGetRequired<Profile | null>(
      "/api/profiles/active",
      (value): value is Profile | null => value === null || isProfile(value),
    ),
    apiGetRequired<Evaluation | null>(
      "/api/profiles/evaluation",
      (value): value is Evaluation | null =>
        value === null || isEvaluation(value),
    ),
    apiGetRequired<IntelligenceStatus | null>(
      "/api/intelligence/status",
      (value): value is IntelligenceStatus | null =>
        value === null || isIntelligenceStatus(value),
    ),
  ]);
  if (!profile) {
    return <><p className="eyebrow">Style intelligence</p><h1>No profile yet.</h1><section className="panel empty"><div><strong>Analyze the fixture catalogue first.</strong>Run <code>runway analyze history --resume</code>, then <code>runway profile build</code>.</div></section></>;
  }
  return (
    <>
      <header className="page-header">
        <div><p className="eyebrow">Qlob profile · v{profile.version}</p><h1>What Runway has learned.</h1><p className="lede">This retrieval profile comes from local statistics and cited examples. It is separate from model-weight training.</p></div>
        <div className="header-counter"><strong>{profile.training_post_ids.length}</strong><span>training records</span></div>
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
      {intelligence && (
        <section className="panel intelligence-readiness">
          <div>
            <p className="eyebrow">Creator learning</p>
            <h2>
              {intelligence.state === "active"
                ? "Preference model active"
                : intelligence.state === "ready_to_train"
                  ? "Labels ready for an evaluated training run"
                  : "Collecting your decisions"}
            </h2>
            <p>
              Generator decisions already affect retrieval immediately. Learned model
              weights activate only after held-out evaluation passes.
            </p>
          </div>
          <dl>
            <div>
              <dt>Feedback signals</dt>
              <dd>{intelligence.feedback_signals.total}</dd>
            </div>
            <div>
              <dt>Human comparisons</dt>
              <dd>{intelligence.human_pairwise_labels}</dd>
            </div>
            <div>
              <dt>Active model targets</dt>
              <dd>{intelligence.active_model_targets.length} / 3</dd>
            </div>
            <div>
              <dt>Blind-study responses</dt>
              <dd>
                {intelligence.blind_study_responses} /{" "}
                {intelligence.blind_study_target}
              </dd>
            </div>
          </dl>
          <small>
            Training evidence · caption{" "}
            {intelligence.training_pairwise_labels_by_target.caption}/
            {intelligence.training_minimum_labels_by_target.caption} · image{" "}
            {intelligence.training_pairwise_labels_by_target.image}/
            {intelligence.training_minimum_labels_by_target.image} · pairing{" "}
            {intelligence.training_pairwise_labels_by_target.pairing}/
            {intelligence.training_minimum_labels_by_target.pairing}. Ready targets:{" "}
            {intelligence.trainable_targets.join(", ") || "none yet"}. The creator
            blind-study gate remains {intelligence.blind_study_target} cases.
          </small>
        </section>
      )}
      <section className="panel"><p className="eyebrow">Representative images + captions</p><div className="quote-list">{profile.representative_positive_examples.map((example) => <Link href={`/catalogue/${example.post_id}`} key={example.post_id}>{example.media_url && <img src={`${process.env.NEXT_PUBLIC_RUNWAY_API_URL ?? "http://127.0.0.1:8000"}${example.media_url}`} alt="" />}<span>#{example.post_id}</span><q>{example.caption}</q></Link>)}</div></section>
      <section className="panel"><p className="eyebrow">Rotation observations</p><ul className="clean-list">{profile.rotation_patterns.map((item) => <li key={item}>{item}</li>)}</ul></section>
      {evaluation?.profile_version === profile.version && <section className="panel"><p className="eyebrow">Measured holdout evaluation · v{evaluation.profile_version}</p><div className="evaluation-grid"><div><span>Image-caption matching</span><strong><Percent value={evaluation.image_caption_matching.accuracy} /></strong></div><div><span>Caption ranking</span><strong><Percent value={evaluation.qlob_caption_ranking_accuracy} /></strong></div><div><span>Retrieval relevance</span><strong><Percent value={evaluation.retrieval_top3_franchise_relevance} /></strong></div><div><span>Duplicate recall</span><strong><Percent value={evaluation.duplicate_detection.transformed_true_positive_rate} /></strong></div></div><small>Holdout metrics diagnose retrieval behavior; every generated caption still requires human judgment.</small></section>}
    </>
  );
}
