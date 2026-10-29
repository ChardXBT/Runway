"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { PlatformBadge, platformLabel } from "@/components/platform-badge";
import { StatePanel } from "@/components/state-panel";
import { API_URL } from "@/lib/api";
import { actionError, readApiJson } from "@/lib/client-api";
import { formatAbsoluteTime, formatHandle } from "@/lib/format";
import { isDraftList } from "@/lib/guards";
import type {
  DraftView,
  PlatformKind,
  PlatformsResponse,
} from "@/lib/types";

const PLATFORM_ORDER: PlatformKind[] = ["youtube", "instagram", "tiktok"];
const CAPTION_LIMITS: Record<PlatformKind, string> = {
  youtube: "YouTube title 100 / description 5,000",
  instagram: "Instagram caption 2,200",
  tiktok: "TikTok caption 2,200",
};
const STATUS_LABELS: Record<string, string> = {
  draft: "Draft",
  scheduled: "Scheduled",
  published: "Published",
  partially_published: "Partially published",
  pending: "Pending",
};
const GROUPS: { key: string; label: string; match: (draft: DraftView) => boolean }[] = [
  {
    key: "scheduled",
    label: "Scheduled",
    match: (draft) => draft.status === "scheduled",
  },
  { key: "draft", label: "Drafts", match: (draft) => draft.status === "draft" },
  {
    key: "partial",
    label: "Partially published",
    match: (draft) => draft.status === "partially_published",
  },
  {
    key: "published",
    label: "Published",
    match: (draft) => draft.status === "published",
  },
];

function isDraft(value: unknown): value is DraftView {
  return isDraftList([value]);
}

function toLocalInput(value: string | null) {
  if (value === null) return "";
  const date = new Date(value.endsWith("Z") || /[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function DraftsWorkspace({
  platforms,
  drafts: initialDrafts,
}: {
  platforms: PlatformsResponse;
  drafts: DraftView[];
  timezone: string;
}) {
  const [drafts, setDrafts] = useState<DraftView[]>(initialDrafts);
  const [error, setError] = useState<string | null>(null);

  const connectedAccounts = useMemo(
    () =>
      new Map(
        PLATFORM_ORDER.map((platform) => {
          const entry = platforms.platforms.find(
            (item) => item.platform === platform,
          );
          const account = entry?.accounts.find(
            (item) => item.status === "connected",
          );
          return [platform, account ?? null] as const;
        }),
      ),
    [platforms],
  );

  const accountHandle = (accountId: number | null) => {
    if (accountId === null) return null;
    for (const entry of platforms.platforms) {
      const account = entry.accounts.find((item) => item.id === accountId);
      if (account) return formatHandle(account.handle) || account.display_name;
    }
    return null;
  };

  const replaceDraft = (draft: DraftView) =>
    setDrafts((current) =>
      current.map((item) => (item.id === draft.id ? draft : item)),
    );

  return (
    <div className="drafts-workspace">
      <header className="page-header">
        <div className="page-header-copy">
          <p className="eyebrow">Create and schedule</p>
          <h1>Drafts</h1>
          <p className="lede">
            Plan one piece of content for several destinations. Runway records
            where it was published; it does not publish videos, Reels, or
            TikToks in this release.
          </p>
        </div>
      </header>

      <div className="drafts-body">
        <NewDraftForm
          connectedAccounts={connectedAccounts}
          onCreated={(draft) => setDrafts((current) => [draft, ...current])}
          onError={setError}
        />
        <div className="drafts-groups">
          {error !== null ? (
            <p className="notice error-notice" role="alert">
              {error}
            </p>
          ) : null}
          {drafts.length === 0 ? (
            <StatePanel
              variant="empty"
              title="No drafts yet"
              body="Drafts collect the caption and plan for one piece of content across platforms. Create one, publish it yourself on each platform, then mark each target as published so Runway can link the synced post."
            />
          ) : (
            GROUPS.map((group) => {
              const items = drafts.filter(group.match);
              if (items.length === 0) return null;
              return (
                <section key={group.key} className="drafts-group">
                  <h2 className="drafts-group-title">
                    {group.label}
                    <span className="panel-hint"> · {items.length}</span>
                  </h2>
                  {items.map((draft) => (
                    <DraftCard
                      key={draft.id}
                      draft={draft}
                      accountHandle={accountHandle}
                      onChange={replaceDraft}
                      onArchived={(id) =>
                        setDrafts((current) =>
                          current.filter((item) => item.id !== id),
                        )
                      }
                    />
                  ))}
                </section>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}

function NewDraftForm({
  connectedAccounts,
  onCreated,
  onError,
}: {
  connectedAccounts: Map<PlatformKind, { id: number; handle: string | null; display_name: string | null } | null>;
  onCreated: (draft: DraftView) => void;
  onError: (message: string | null) => void;
}) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [scheduledFor, setScheduledFor] = useState("");
  const [selected, setSelected] = useState<Set<PlatformKind>>(new Set());
  const [captions, setCaptions] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const toggle = (platform: PlatformKind) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(platform)) {
        next.delete(platform);
      } else {
        next.add(platform);
      }
      return next;
    });
  };

  const submit = async () => {
    setSubmitting(true);
    setFormError(null);
    onError(null);
    try {
      const targets = [...selected].map((platform) => ({
        platform,
        account_id: connectedAccounts.get(platform)?.id ?? null,
        caption_override: captions[platform]?.trim() || null,
      }));
      const response = await fetch(`${API_URL}/api/drafts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: title.trim(),
          body,
          scheduled_for: scheduledFor || null,
          targets,
        }),
      });
      const draft = await readApiJson(response, {
        validate: isDraft,
        failureMessage: "Could not create the draft.",
      });
      onCreated(draft);
      setTitle("");
      setBody("");
      setScheduledFor("");
      setSelected(new Set());
      setCaptions({});
    } catch (err) {
      setFormError(actionError(err, "Could not create the draft."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="panel new-draft">
      <div className="section-heading">
        <h2>New draft</h2>
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label>
          Title
          <input
            value={title}
            required
            maxLength={300}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="What is this piece about?"
          />
        </label>
        <label>
          Body / caption draft
          <textarea
            value={body}
            rows={5}
            maxLength={20000}
            onChange={(event) => setBody(event.target.value)}
          />
        </label>
        <label>
          Schedule
          <input
            type="datetime-local"
            value={scheduledFor}
            onChange={(event) => setScheduledFor(event.target.value)}
          />
          <span className="field-hint">
            Times are interpreted in your browser’s local timezone.
          </span>
        </label>
        <fieldset className="draft-targets">
          <legend>Targets</legend>
          {PLATFORM_ORDER.map((platform) => {
            const account = connectedAccounts.get(platform) ?? null;
            const checked = selected.has(platform);
            return (
              <div key={platform} className="draft-target">
                <label className="draft-target-check">
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(platform)}
                  />
                  <PlatformBadge platform={platform} size="sm" />
                  <span className="draft-target-account">
                    {account !== null
                      ? formatHandle(account.handle) ||
                        account.display_name ||
                        "account"
                      : "no account connected"}
                  </span>
                </label>
                {checked ? (
                  <label className="draft-target-caption">
                    Caption override
                    <textarea
                      rows={2}
                      maxLength={2200}
                      value={captions[platform] ?? ""}
                      onChange={(event) =>
                        setCaptions((current) => ({
                          ...current,
                          [platform]: event.target.value,
                        }))
                      }
                    />
                    <span className="field-hint">
                      {(captions[platform] ?? "").length} characters ·{" "}
                      {CAPTION_LIMITS[platform]}
                    </span>
                  </label>
                ) : null}
              </div>
            );
          })}
        </fieldset>
        <button
          type="submit"
          className="button"
          disabled={submitting || title.trim() === ""}
        >
          {submitting ? "Creating…" : "Create draft"}
        </button>
        {formError !== null ? (
          <p className="notice error-notice" role="alert">
            {formError}
          </p>
        ) : null}
      </form>
    </section>
  );
}

function DraftCard({
  draft,
  accountHandle,
  onChange,
  onArchived,
}: {
  draft: DraftView;
  accountHandle: (accountId: number | null) => string | null;
  onChange: (draft: DraftView) => void;
  onArchived: (id: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(draft.title);
  const [body, setBody] = useState(draft.body);
  const [scheduledFor, setScheduledFor] = useState(
    toLocalInput(draft.scheduled_for),
  );
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const saveEdit = async () => {
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`${API_URL}/api/drafts/${draft.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: title.trim(),
          body,
          scheduled_for: scheduledFor || null,
          clear_schedule: scheduledFor === "",
        }),
      });
      const updated = await readApiJson(response, {
        validate: isDraft,
        failureMessage: "Could not save the draft.",
      });
      onChange(updated);
      setEditing(false);
    } catch (err) {
      setError(actionError(err, "Could not save the draft."));
    } finally {
      setSaving(false);
    }
  };

  const archive = async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`${API_URL}/api/drafts/${draft.id}`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error("archive failed");
      onArchived(draft.id);
    } catch (err) {
      setError(actionError(err, "Could not archive the draft."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <article className="panel draft-card" id={`draft-${draft.id}`}>
      <div className="section-heading">
        <h3>{draft.title}</h3>
        <span className={`status status-${draft.status}`}>
          {STATUS_LABELS[draft.status] ?? draft.status}
        </span>
      </div>
      {draft.scheduled_for !== null ? (
        <p className="draft-scheduled">
          Scheduled {formatAbsoluteTime(draft.scheduled_for)} (browser local
          time)
        </p>
      ) : null}
      {draft.body !== "" ? <p className="draft-body">{draft.body}</p> : null}

      {draft.targets.length > 0 ? (
        <ul className="draft-target-list">
          {draft.targets.map((target) => (
            <TargetRow
              key={target.id}
              draft={draft}
              target={target}
              handle={accountHandle(target.account_id)}
              onChange={onChange}
            />
          ))}
        </ul>
      ) : (
        <p className="panel-hint">No platform targets yet.</p>
      )}

      <div className="draft-actions">
        {editing ? (
          <div className="draft-edit">
            <label>
              Title
              <input
                value={title}
                maxLength={300}
                onChange={(event) => setTitle(event.target.value)}
              />
            </label>
            <label>
              Body
              <textarea
                value={body}
                rows={4}
                maxLength={20000}
                onChange={(event) => setBody(event.target.value)}
              />
            </label>
            <label>
              Schedule
              <input
                type="datetime-local"
                value={scheduledFor}
                onChange={(event) => setScheduledFor(event.target.value)}
              />
              <span className="field-hint">
                Times use your browser’s local timezone. Clear to unschedule.
              </span>
            </label>
            <div className="draft-edit-actions">
              <button
                type="button"
                className="button"
                onClick={saveEdit}
                disabled={saving || title.trim() === ""}
              >
                {saving ? "Saving…" : "Save"}
              </button>
              <button
                type="button"
                className="button secondary"
                onClick={() => setEditing(false)}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            className="button secondary"
            onClick={() => {
              setTitle(draft.title);
              setBody(draft.body);
              setScheduledFor(toLocalInput(draft.scheduled_for));
              setEditing(true);
            }}
          >
            Edit
          </button>
        )}
        {confirmArchive ? (
          <span className="draft-archive-confirm" role="group" aria-label="Confirm archive">
            <button
              type="button"
              className="button"
              onClick={archive}
              disabled={saving}
            >
              {saving ? "Archiving…" : "Confirm archive"}
            </button>
            <button
              type="button"
              className="button secondary"
              onClick={() => setConfirmArchive(false)}
              disabled={saving}
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="button secondary"
            onClick={() => setConfirmArchive(true)}
          >
            Archive
          </button>
        )}
      </div>
      {error !== null ? (
        <p className="notice error-notice" role="alert">
          {error}
        </p>
      ) : null}
    </article>
  );
}

function TargetRow({
  draft,
  target,
  handle,
  onChange,
}: {
  draft: DraftView;
  target: DraftView["targets"][number];
  handle: string | null;
  onChange: (draft: DraftView) => void;
}) {
  const [marking, setMarking] = useState(false);
  const [url, setUrl] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const capability = target.publish_capability;
  const stateLabel =
    capability.state === "supported_not_implemented"
      ? "Not implemented"
      : capability.state === "approval_required"
        ? "Approval required"
        : capability.state === "unsupported"
          ? "Unsupported"
          : "Unavailable";

  const markPublished = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const response = await fetch(
        `${API_URL}/api/drafts/${draft.id}/targets/${target.id}/mark-published`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ published_url: url.trim(), confirmed: true }),
        },
      );
      const updated = await readApiJson(response, {
        validate: isDraft,
        failureMessage: "Could not mark the target as published.",
      });
      const updatedTarget = updated.targets.find((item) => item.id === target.id);
      if (updatedTarget?.published_content_id != null) {
        setMessage("Published · linked to imported content.");
      } else {
        setMessage(
          "Published · not yet matched to imported content (will link after the next sync).",
        );
      }
      onChange(updated);
      setMarking(false);
      setConfirming(false);
    } catch (err) {
      setMessage(actionError(err, "Could not mark the target as published."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <li className="draft-target-row">
      <PlatformBadge platform={target.platform} />
      <span className="draft-target-handle">
        {handle !== null ? handle : "no account"}
      </span>
      <span className={`status status-${target.status}`}>
        {STATUS_LABELS[target.status] ?? target.status}
      </span>
      {target.status === "published" ? (
        <span className="draft-target-result">
          {target.published_content_id !== null ? (
            <>
              Published ·{" "}
              <Link href={`/content/${target.published_content_id}`}>
                linked to content
              </Link>
            </>
          ) : (
            "Published · not yet matched to imported content (will link after the next sync)"
          )}
        </span>
      ) : (
        <>
          <button
            type="button"
            className="button secondary"
            disabled
            aria-disabled="true"
            title={capability.detail}
          >
            Publish
          </button>
          <span className="draft-target-capability">{stateLabel} — {capability.detail}</span>
          {marking ? (
            confirming ? (
              <span className="draft-mark-confirm" role="group" aria-label="Confirm published">
                <input
                  type="url"
                  value={url}
                  placeholder={`${platformLabel(target.platform)} post URL`}
                  onChange={(event) => setUrl(event.target.value)}
                />
                <button
                  type="button"
                  className="button"
                  onClick={markPublished}
                  disabled={saving || url.trim() === ""}
                >
                  {saving ? "Saving…" : "Confirm"}
                </button>
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => {
                    setConfirming(false);
                    setMarking(false);
                  }}
                >
                  Cancel
                </button>
              </span>
            ) : (
              <span className="draft-mark-confirm">
                <input
                  type="url"
                  value={url}
                  placeholder={`${platformLabel(target.platform)} post URL`}
                  onChange={(event) => setUrl(event.target.value)}
                />
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => setConfirming(true)}
                  disabled={url.trim() === ""}
                >
                  Confirm
                </button>
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => setMarking(false)}
                >
                  Cancel
                </button>
              </span>
            )
          ) : (
            <button
              type="button"
              className="button secondary"
              onClick={() => setMarking(true)}
            >
              Mark as published
            </button>
          )}
        </>
      )}
      {message !== null ? (
        <span className="draft-target-message" role="status">
          {message}
        </span>
      ) : null}
    </li>
  );
}
