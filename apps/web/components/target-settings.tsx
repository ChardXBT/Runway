"use client";

/** Per-post-type settings panels (spec 08).
 *
 * Each panel renders the fields the backend's `TargetSettings` model accepts
 * for that post type and emits a plain settings object — only the model's own
 * keys, so the strict `extra=forbid` parser never sees a surprise. Required
 * fields (YouTube privacy / made_for_kids / paid placement / category) render
 * with no default chosen; the server's issue list stays the source of truth.
 */

import { useEffect, useState } from "react";

import { actionError } from "@/lib/client-api";
import { INSTAGRAM_COLLABORATORS_MAX, YOUTUBE_TITLE_MAX, YOUTUBE_DESCRIPTION_MAX } from "@/lib/caption-stats";
import { V1, V1ApiError } from "@/lib/v1-client";

export type SettingsMap = Record<string, unknown>;

function Field({
  label,
  hint,
  children,
  htmlFor,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
  htmlFor?: string;
}) {
  return (
    <div className="field">
      {htmlFor ? <label htmlFor={htmlFor}>{label}</label> : <span className="field-label">{label}</span>}
      {children}
      {hint ? <p className="field-hint">{hint}</p> : null}
    </div>
  );
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function strList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function boolOrUndef(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

// ---------------------------------------------------------------------------
// YouTube video / short

export function YouTubeVideoSettings({
  accountId,
  settings,
  onChange,
}: {
  accountId: number | null;
  settings: SettingsMap;
  onChange: (next: SettingsMap) => void;
}) {
  const [categories, setCategories] = useState<{ id: string; title: string }[] | null>(null);
  const [categoriesNote, setCategoriesNote] = useState<string | null>(null);
  const set = (key: string, value: unknown) => onChange({ ...settings, [key]: value });

  useEffect(() => {
    if (accountId === null) return;
    let cancelled = false;
    V1.accounts
      .youtubeCategories(accountId)
      .then((result) => {
        if (cancelled) return;
        setCategories(result.items.filter((item) => item.id));
        setCategoriesNote(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setCategories(null);
        // 404 (route missing on an older API) and capability errors both land
        // here; the owner can still type a category id by hand.
        setCategoriesNote(
          actionError(err, "Categories could not be loaded; enter a category id by hand."),
        );
      });
    return () => {
      cancelled = true;
    };
  }, [accountId]);

  const title = str(settings.title);
  const description = str(settings.description);
  const tagsText = strList(settings.tags).join(", ");
  const privacy = str(settings.privacy);
  const scheduleMode = str(settings.schedule_mode);

  return (
    <div className="target-settings youtube-settings">
      <Field label="Title" htmlFor="yt-title" hint={`YouTube titles are limited to ${YOUTUBE_TITLE_MAX} characters.`}>
        <input
          id="yt-title"
          type="text"
          value={title}
          onChange={(event) => set("title", event.target.value || null)}
          maxLength={1000}
        />
        <small className={title.length > YOUTUBE_TITLE_MAX ? "text-danger" : undefined}>
          {title.length}/{YOUTUBE_TITLE_MAX}
        </small>
      </Field>
      <Field label="Description" htmlFor="yt-desc" hint={`Empty falls back to the caption. Max ${YOUTUBE_DESCRIPTION_MAX}.`}>
        <textarea
          id="yt-desc"
          value={description}
          rows={4}
          onChange={(event) => set("description", event.target.value || null)}
        />
        <small className={description.length > YOUTUBE_DESCRIPTION_MAX ? "text-danger" : undefined}>
          {description.length}/{YOUTUBE_DESCRIPTION_MAX}
        </small>
      </Field>
      <Field label="Tags" htmlFor="yt-tags" hint="Comma-separated; 500 characters in total.">
        <input
          id="yt-tags"
          type="text"
          value={tagsText}
          onChange={(event) =>
            set(
              "tags",
              event.target.value
                .split(",")
                .map((tag) => tag.trim())
                .filter(Boolean),
            )
          }
        />
      </Field>
      <Field label="Category" htmlFor="yt-category">
        {categories && categories.length > 0 ? (
          <select
            id="yt-category"
            value={str(settings.category_id)}
            onChange={(event) => set("category_id", event.target.value || null)}
          >
            <option value="">Choose a category…</option>
            {categories.map((category) => (
              <option key={category.id} value={category.id}>
                {category.title}
              </option>
            ))}
          </select>
        ) : (
          <input
            id="yt-category"
            type="text"
            inputMode="numeric"
            placeholder="Category id"
            value={str(settings.category_id)}
            onChange={(event) => set("category_id", event.target.value || null)}
          />
        )}
        {categoriesNote ? <p className="field-hint">{categoriesNote}</p> : null}
      </Field>
      <Field label="Who can see it" htmlFor="yt-privacy">
        <select
          id="yt-privacy"
          value={privacy}
          onChange={(event) => set("privacy", event.target.value || null)}
        >
          <option value="">Choose — never guessed</option>
          <option value="public">Public: anyone can find and watch it</option>
          <option value="unlisted">Unlisted: anyone with the link can watch it</option>
          <option value="private">Private: only you can watch it</option>
        </select>
      </Field>
      <div className="two-fields">
        <Field label="Made for kids?" htmlFor="yt-kids">
          <select
            id="yt-kids"
            value={settings.made_for_kids === true ? "yes" : settings.made_for_kids === false ? "no" : ""}
            onChange={(event) =>
              set("made_for_kids", event.target.value === "" ? null : event.target.value === "yes")
            }
          >
            <option value="">Choose…</option>
            <option value="yes">Yes, made for kids</option>
            <option value="no">No, not made for kids</option>
          </select>
        </Field>
        <Field label="Paid product placement?" htmlFor="yt-ppp">
          <select
            id="yt-ppp"
            value={
              settings.has_paid_product_placement === true
                ? "yes"
                : settings.has_paid_product_placement === false
                  ? "no"
                  : ""
            }
            onChange={(event) =>
              set(
                "has_paid_product_placement",
                event.target.value === "" ? null : event.target.value === "yes",
              )
            }
          >
            <option value="">Choose…</option>
            <option value="yes">Yes, it has paid placement</option>
            <option value="no">No paid placement</option>
          </select>
        </Field>
      </div>
      <Field label="Synthetic media?" htmlFor="yt-synthetic" hint="Required when the video contains realistic altered or generated media.">
        <select
          id="yt-synthetic"
          value={
            settings.contains_synthetic_media === true
              ? "yes"
              : settings.contains_synthetic_media === false
                ? "no"
                : ""
          }
          onChange={(event) =>
            set(
              "contains_synthetic_media",
              event.target.value === "" ? null : event.target.value === "yes",
            )
          }
        >
          <option value="">Choose…</option>
          <option value="yes">Yes, contains synthetic media</option>
          <option value="no">No synthetic media</option>
        </select>
      </Field>
      <Field label="Scheduling" htmlFor="yt-schedule-mode">
        <select
          id="yt-schedule-mode"
          value={scheduleMode}
          onChange={(event) => set("schedule_mode", event.target.value || null)}
        >
          <option value="">Choose…</option>
          <option value="native">YouTube native schedule (needs lead time)</option>
          <option value="held">Held: Runway publishes at the time</option>
        </select>
      </Field>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Instagram

function CollaboratorsField({
  settings,
  onChange,
}: {
  settings: SettingsMap;
  onChange: (next: SettingsMap) => void;
}) {
  const collaborators = strList(settings.collaborators);
  return (
    <Field
      label="Collaborators"
      htmlFor="ig-collaborators"
      hint={`Comma-separated usernames, at most ${INSTAGRAM_COLLABORATORS_MAX}.`}
    >
      <input
        id="ig-collaborators"
        type="text"
        value={collaborators.join(", ")}
        onChange={(event) =>
          onChange({
            ...settings,
            collaborators: event.target.value
              .split(",")
              .map((name) => name.trim().replace(/^@/, ""))
              .filter(Boolean),
          })
        }
      />
      {collaborators.length > INSTAGRAM_COLLABORATORS_MAX ? (
        <small className="text-danger">At most {INSTAGRAM_COLLABORATORS_MAX} collaborators.</small>
      ) : null}
    </Field>
  );
}

function AiGeneratedField({
  settings,
  onChange,
}: {
  settings: SettingsMap;
  onChange: (next: SettingsMap) => void;
}) {
  const value = settings.is_ai_generated;
  return (
    <Field label="AI-generated content?" htmlFor="ig-aigc">
      <select
        id="ig-aigc"
        value={value === true ? "yes" : value === false ? "no" : ""}
        onChange={(event) =>
          onChange({
            ...settings,
            is_ai_generated: event.target.value === "" ? null : event.target.value === "yes",
          })
        }
      >
        <option value="">Follow the post setting</option>
        <option value="yes">Yes, label it as AI-generated</option>
        <option value="no">No AI label</option>
      </select>
    </Field>
  );
}

function LocationField({
  settings,
  onChange,
}: {
  settings: SettingsMap;
  onChange: (next: SettingsMap) => void;
}) {
  return (
    <Field label="Location id" htmlFor="ig-location" hint="A Facebook location id; leave empty for none.">
      <input
        id="ig-location"
        type="text"
        inputMode="numeric"
        value={str(settings.location_id)}
        onChange={(event) =>
          onChange({ ...settings, location_id: event.target.value || null })
        }
      />
    </Field>
  );
}

export function InstagramReelSettings({
  settings,
  onChange,
}: {
  settings: SettingsMap;
  onChange: (next: SettingsMap) => void;
}) {
  return (
    <div className="target-settings instagram-settings">
      <Field label="Also share to feed?" htmlFor="ig-share">
        <select
          id="ig-share"
          value={settings.share_to_feed === true ? "yes" : settings.share_to_feed === false ? "no" : ""}
          onChange={(event) =>
            onChange({
              ...settings,
              share_to_feed: event.target.value === "" ? null : event.target.value === "yes",
            })
          }
        >
          <option value="">Choose…</option>
          <option value="yes">Yes, share to the feed too</option>
          <option value="no">Reel only</option>
        </select>
      </Field>
      <Field label="Audio name" htmlFor="ig-audio" hint="Optional audio attribution.">
        <input
          id="ig-audio"
          type="text"
          value={str(settings.audio_name)}
          onChange={(event) => onChange({ ...settings, audio_name: event.target.value || null })}
        />
      </Field>
      <Field label="Thumbnail offset (ms)" htmlFor="ig-thumb" hint="Either an offset or a cover image — never both.">
        <input
          id="ig-thumb"
          type="number"
          min={0}
          value={typeof settings.thumb_offset_ms === "number" ? settings.thumb_offset_ms : ""}
          onChange={(event) =>
            onChange({
              ...settings,
              thumb_offset_ms: event.target.value === "" ? null : Number(event.target.value),
            })
          }
        />
      </Field>
      <CollaboratorsField settings={settings} onChange={onChange} />
      <AiGeneratedField settings={settings} onChange={onChange} />
    </div>
  );
}

export function InstagramImageSettings({
  settings,
  onChange,
}: {
  settings: SettingsMap;
  onChange: (next: SettingsMap) => void;
}) {
  const altText = str(settings.alt_text);
  return (
    <div className="target-settings instagram-settings">
      <Field label="Alt text" htmlFor="ig-alt" hint="Accessibility text; at most 1000 characters.">
        <textarea
          id="ig-alt"
          rows={2}
          value={altText}
          onChange={(event) => onChange({ ...settings, alt_text: event.target.value || null })}
        />
        <small className={altText.length > 1000 ? "text-danger" : undefined}>{altText.length}/1000</small>
      </Field>
      <LocationField settings={settings} onChange={onChange} />
      <CollaboratorsField settings={settings} onChange={onChange} />
      <AiGeneratedField settings={settings} onChange={onChange} />
    </div>
  );
}

export function InstagramCarouselSettings({
  settings,
  onChange,
}: {
  settings: SettingsMap;
  onChange: (next: SettingsMap) => void;
}) {
  return (
    <div className="target-settings instagram-settings">
      <p className="field-hint">The carousel children are the post&apos;s media in order.</p>
      <LocationField settings={settings} onChange={onChange} />
      <CollaboratorsField settings={settings} onChange={onChange} />
      <AiGeneratedField settings={settings} onChange={onChange} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dispatcher

export function TargetSettingsPanel({
  postType,
  accountId,
  platform,
  settings,
  onChange,
  captionLength,
  media,
}: {
  postType: string | null;
  accountId: number | null;
  platform: string;
  settings: SettingsMap;
  onChange: (next: SettingsMap) => void;
  captionLength: number;
  media: { asset_id: number; kind: string | null }[];
}) {
  void media;
  if (postType === "youtube_video" || postType === "youtube_short") {
    return <YouTubeVideoSettings accountId={accountId} settings={settings} onChange={onChange} />;
  }
  if (postType === "instagram_reel") {
    return <InstagramReelSettings settings={settings} onChange={onChange} />;
  }
  if (postType === "instagram_image") {
    return <InstagramImageSettings settings={settings} onChange={onChange} />;
  }
  if (postType === "instagram_carousel") {
    return <InstagramCarouselSettings settings={settings} onChange={onChange} />;
  }
  if (postType === "instagram_story") {
    return (
      <p className="field-hint">
        Instagram Stories have no caption or extra settings; the media is the Story.
      </p>
    );
  }
  if (postType === "youtube_post") {
    return (
      <p className="field-hint">
        Community posts carry the caption text plus the post&apos;s images — no extra settings.
      </p>
    );
  }
  if (postType === "tiktok_draft") {
    return (
      <p className="field-hint">
        Sends a draft to the TikTok inbox; the creator finishes privacy and posting in the app.
        {captionLength > 0 ? "" : ""}
      </p>
    );
  }
  return null;
}
