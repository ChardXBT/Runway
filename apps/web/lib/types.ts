export type CandidateTopic = {
  franchise?: string | null;
  characters?: string[];
  scene_archetype?: string;
  composition?: string;
  emotion?: string;
};

export type Proposal = {
  id: number;
  generation_run_id: number;
  planned_publish_at: string;
  status: string;
  candidate_image_id: number;
  backup_candidate_ids: number[];
  recommended_caption: string;
  alternative_captions: string[];
  final_caption: string;
  selection_reason: string;
  scores: { style: number; novelty: number; quality: number };
  closest_historical_matches: {
    post_id: number;
    caption: string;
    published_at: string | null;
    visual_similarity: number;
    media_url?: string;
  }[];
  warnings: string[];
  candidate: {
    original_url: string | null;
    preview_url: string | null;
    source_page_url: string | null;
    direct_image_url: string | null;
    source_domain: string | null;
    rights_status: string;
    detected_topic: CandidateTopic;
  } | null;
};
