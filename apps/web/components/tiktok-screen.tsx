"use client";

/** TikTok's mandatory posting screen (spec 08 §4, research/tiktok §7).
 *
 * Built fresh from `GET /accounts/{id}/tiktok/creator-info` every time the
 * panel mounts — never from cached values. Privacy is never preselected;
 * comment/duet/stitch start off and are disabled (greyed) when the creator
 * disabled them; commercial content starts off and needs at least one of
 * your_brand / branded_content; branded content cannot be SELF_ONLY.
 */

import { useEffect, useState } from "react";

import { actionError } from "@/lib/client-api";
import { V1 } from "@/lib/v1-client";
import type { TikTokCreatorInfo } from "@/lib/v1-guards";
import type { SettingsMap } from "@/components/target-settings";

const PRIVACY_LABELS: Record<string, string> = {
  PUBLIC_TO_EVERYONE: "Everyone",
  MUTUAL_FOLLOW_FRIENDS: "Friends (followers you follow back)",
  FOLLOWER_OF_CREATOR: "Followers",
  SELF_ONLY: "Only you",
};

export function tiktokScreenIssues(settings: SettingsMap): string[] {
  const issues: string[] = [];
  if (!settings.privacy_level) {
    issues.push("Choose who can see the video — TikTok requires the creator to pick it.");
  }
  if (settings.commercial_content === true) {
    if (settings.your_brand !== true && settings.branded_content !== true) {
      issues.push(
        "You need to indicate if your content promotes yourself, a third party, or both.",
      );
    }
    if (settings.branded_content === true && settings.privacy_level === "SELF_ONLY") {
      issues.push("Branded content visibility cannot be set to private.");
    }
  }
  return issues;
}

export function TikTokScreen({
  accountId,
  settings,
  onChange,
  captionLength,
  aiGenerated,
}: {
  accountId: number | null;
  settings: SettingsMap;
  onChange: (next: SettingsMap) => void;
  captionLength: number;
  aiGenerated: boolean;
}) {
  const [info, setInfo] = useState<TikTokCreatorInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadedFor, setLoadedFor] = useState<number | null>(null);

  // Render-phase adjustment: a new account discards the previous creator info.
  if (loadedFor !== accountId) {
    setLoadedFor(accountId);
    setInfo(null);
    setError(null);
  }

  useEffect(() => {
    if (accountId === null) return;
    let cancelled = false;
    V1.accounts
      .tiktokCreatorInfo(accountId)
      .then((result) => {
        if (cancelled) return;
        setInfo(result);
      })
      .catch((err) => {
        if (cancelled) return;
        setInfo(null);
        setError(
          actionError(
            err,
            "TikTok's creator options could not be loaded; the video cannot be prepared without them.",
          ),
        );
      });
    return () => {
      cancelled = true;
    };
  }, [accountId]);

  const set = (key: string, value: unknown) => onChange({ ...settings, [key]: value });
  const commercial = settings.commercial_content === true;
  const branded = settings.branded_content === true;
  const privacy = typeof settings.privacy_level === "string" ? settings.privacy_level : "";
  const issues = tiktokScreenIssues(settings);
  const consent = branded ? (info?.consent.branded ?? info?.consent.default) : info?.consent.default;

  if (accountId === null) {
    return (
      <p className="notice error-notice" role="alert">
        Choose a TikTok account to load its creator options.
      </p>
    );
  }
  if (info === null && error === null) {
    return <p className="field-hint">Loading TikTok creator options…</p>;
  }
  if (error || !info) {
    return (
      <p className="notice error-notice" role="alert">
        {error ?? "TikTok's creator options are unavailable."}
      </p>
    );
  }

  const nickname = info.creator_nickname ?? info.creator_username ?? "this creator";
  return (
    <div className="target-settings tiktok-screen" data-testid="tiktok-screen">
      <p className="tiktok-creator">
        Posting as <strong>{nickname}</strong>
        {info.creator_username ? (
          <span className="tiktok-username"> @{info.creator_username}</span>
        ) : null}
      </p>
      {typeof info.max_video_post_duration_sec === "number" ? (
        <p className="field-hint">
          This creator can post videos up to {info.max_video_post_duration_sec}s.
        </p>
      ) : null}
      <div className="field">
        <label htmlFor="tt-privacy">Who can see it</label>
        <select
          id="tt-privacy"
          value={privacy}
          onChange={(event) => set("privacy_level", event.target.value || null)}
        >
          <option value="">Choose — TikTok never assumes</option>
          {info.privacy_level_options.map((option) => (
            <option key={option} value={option}>
              {PRIVACY_LABELS[option] ?? option}
            </option>
          ))}
        </select>
      </div>
      <fieldset className="tiktok-interactions">
        <legend>Interactions</legend>
        {(
          [
            ["allow_comment", "Allow comments", info.comment_disabled],
            ["allow_duet", "Allow duets", info.duet_disabled],
            ["allow_stitch", "Allow stitches", info.stitch_disabled],
          ] as const
        ).map(([key, label, disabled]) => (
          <label key={key} className={disabled ? "switch-label disabled" : "switch-label"}>
            <input
              type="checkbox"
              checked={settings[key] === true}
              disabled={disabled}
              onChange={(event) => set(key, event.target.checked)}
            />
            {label}
            {disabled ? <small> (turned off by this creator)</small> : null}
          </label>
        ))}
      </fieldset>
      <fieldset className="tiktok-commercial">
        <legend>Commercial content</legend>
        <label className="switch-label">
          <input
            type="checkbox"
            checked={commercial}
            onChange={(event) => {
              const on = event.target.checked;
              onChange({
                ...settings,
                commercial_content: on,
                ...(on ? {} : { your_brand: false, branded_content: false }),
              });
            }}
          />
          This is commercial content
        </label>
        {commercial ? (
          <div className="tiktok-commercial-kinds">
            <label className="switch-label">
              <input
                type="checkbox"
                checked={settings.your_brand === true}
                onChange={(event) => set("your_brand", event.target.checked)}
              />
              Promotes your own brand
            </label>
            <label className="switch-label">
              <input
                type="checkbox"
                checked={branded}
                onChange={(event) => set("branded_content", event.target.checked)}
              />
              Promotes a third party (branded content)
            </label>
            {branded && privacy === "SELF_ONLY" ? (
              <p className="text-danger">Branded content cannot be set to Only you.</p>
            ) : null}
          </div>
        ) : null}
      </fieldset>
      <label className="switch-label">
        <input
          type="checkbox"
          checked={settings.is_aigc === true || (settings.is_aigc === undefined && aiGenerated)}
          onChange={(event) => set("is_aigc", event.target.checked)}
        />
        Label as AI-generated
      </label>
      {issues.length > 0 ? (
        <ul className="tiktok-issues">
          {issues.map((issue) => (
            <li key={issue} className="text-danger">
              {issue}
            </li>
          ))}
        </ul>
      ) : null}
      {consent ? <p className="notice info-notice release-notice">{consent}</p> : null}
      <p className="field-hint">
        TikTok&apos;s own processing can take a few minutes after release. Caption: {captionLength}/2200.
      </p>
    </div>
  );
}
