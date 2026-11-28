/** Caption counters that mirror `post_settings.py` exactly.
 *
 * The backend counts hashtags with `(?<![\w&])#\w+` and mentions with
 * `(?<![\w.])@[\w.]+`, and TikTok captions by UTF-16 code units. These helpers
 * use the same rules so the editor's counters agree with server-side
 * validation instead of approximating it.
 */

export const YOUTUBE_TITLE_MAX = 100;
export const YOUTUBE_DESCRIPTION_MAX = 5000;
export const YOUTUBE_TAGS_TOTAL_MAX = 500;
export const INSTAGRAM_CAPTION_MAX = 2200;
export const INSTAGRAM_HASHTAGS_MAX = 30;
export const INSTAGRAM_MENTIONS_MAX = 20;
export const INSTAGRAM_COLLABORATORS_MAX = 3;
export const INSTAGRAM_ALT_TEXT_MAX = 1000;
export const TIKTOK_TITLE_MAX_UTF16 = 2200;

const HASHTAG_RE = /(?<![\w&])#\w+/g;
const MENTION_RE = /(?<![\w.])@[\w.]+/g;

/** TikTok's unit: UTF-16 code units (`len(text.encode("utf-16-le")) // 2`). */
export function utf16Length(text: string): number {
  return text.length; // JS strings are already UTF-16 code units.
}

export function hashtagCount(text: string): number {
  return text.match(HASHTAG_RE)?.length ?? 0;
}

export function mentionCount(text: string): number {
  return text.match(MENTION_RE)?.length ?? 0;
}

export interface CaptionStats {
  chars: number;
  utf16Chars: number;
  hashtags: number;
  mentions: number;
}

export function captionStats(text: string): CaptionStats {
  return {
    chars: [...text].length,
    utf16Chars: utf16Length(text),
    hashtags: hashtagCount(text),
    mentions: mentionCount(text),
  };
}

export interface CaptionLimit {
  /** Platform limit as a number of units (`kind` says which). */
  max: number;
  kind: "chars" | "utf16";
  label: string;
}

/** The caption limit one post type enforces (undefined = no hard limit). */
export function captionLimitFor(postType: string | null | undefined): CaptionLimit | null {
  switch (postType) {
    case "tiktok_video":
    case "tiktok_draft":
      return { max: TIKTOK_TITLE_MAX_UTF16, kind: "utf16", label: "characters" };
    case "instagram_reel":
    case "instagram_image":
    case "instagram_carousel":
      return { max: INSTAGRAM_CAPTION_MAX, kind: "chars", label: "characters" };
    default:
      return null;
  }
}

export function captionExceeds(postType: string | null | undefined, text: string): boolean {
  const limit = captionLimitFor(postType);
  if (limit === null) return false;
  return limit.kind === "utf16" ? utf16Length(text) > limit.max : [...text].length > limit.max;
}
