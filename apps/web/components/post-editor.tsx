"use client";

/** Post editor for `/posts/new` and `/posts/{id}` (spec 08).
 *
 * Create mode keeps targets local until `POST /api/v1/posts` creates the
 * whole post in one call. Edit mode writes per-target through `PUT` with
 * `If-Match` on the target's version; a 409 means the post changed elsewhere
 * and the editor refetches instead of overwriting.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { PlatformBadge, platformLabel } from "@/components/platform-badge";
import { ReleaseDialog } from "@/components/release-dialog";
import { TargetSettingsPanel, type SettingsMap } from "@/components/target-settings";
import { TargetStatus } from "@/components/target-status";
import { TikTokScreen, tiktokScreenIssues } from "@/components/tiktok-screen";
import { MediaPicker, type PickedMedia } from "@/components/media-picker";
import { actionError } from "@/lib/client-api";
import { captionStats, captionLimitFor } from "@/lib/caption-stats";
import { localDate, localTime, zonedScheduleIso } from "@/lib/datetime";
import { POST_EVENT_TYPES, useEventStream } from "@/lib/event-stream";
import { formatHandle } from "@/lib/format";
import { postTypeLabel, postTypesForPlatform, targetStateCopy } from "@/lib/post-copy";
import { findTarget, V1, V1ApiError } from "@/lib/v1-client";
import type { IssueOut, PostOut, TargetOut } from "@/lib/v1-guards";
import type { AccountView } from "@/lib/types";

export interface EditorAccount {
  id: number;
  platform: string;
  handle: string | null;
  display_name: string | null;
  status: string;
}

export function editorAccounts(accounts: AccountView[]): EditorAccount[] {
  return accounts.map((account) => ({
    id: account.id,
    platform: account.platform,
    handle: account.handle,
    display_name: account.display_name,
    status: account.status,
  }));
}

export function accountLabel(account: EditorAccount | undefined): string {
  if (!account) return "the account";
  return account.display_name ?? formatHandle(account.handle) ?? `account ${account.id}`;
}

function IssueList({ issues }: { issues: IssueOut[] }) {
  if (issues.length === 0) return null;
  return (
    <ul className="issue-list">
      {issues.map((issue, index) => (
        <li key={`${issue.code}-${index}`} className={`issue-${issue.severity}`}>
          {issue.message}
          {issue.fix ? <small> — {issue.fix}</small> : null}
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// One target card (edit mode)

interface TargetDraft {
  postType: string | null;
  captionOverride: string;
  settings: SettingsMap;
  date: string;
  time: string;
}

function draftFromTarget(target: TargetOut, timezone: string): TargetDraft {
  return {
    postType: target.post_type,
    captionOverride: target.caption_override ?? "",
    settings: { ...target.settings },
    date: target.planned_at ? (localDate(target.planned_at, timezone) ?? "") : "",
    time: target.planned_at ? (localTime(target.planned_at, timezone) ?? "") : "",
  };
}

function sameDraft(a: TargetDraft, b: TargetDraft): boolean {
  return (
    a.postType === b.postType &&
    a.captionOverride === b.captionOverride &&
    a.date === b.date &&
    a.time === b.time &&
    JSON.stringify(a.settings) === JSON.stringify(b.settings)
  );
}

function TargetCard({
  post,
  target,
  accounts,
  timezone,
  onChanged,
  onRelease,
}: {
  post: PostOut;
  target: TargetOut;
  accounts: EditorAccount[];
  timezone: string;
  onChanged: (post: PostOut) => void;
  onRelease: (target: TargetOut) => void;
}) {
  const [draft, setDraft] = useState<TargetDraft>(() => draftFromTarget(target, timezone));
  // The server state the draft was last seeded from or acknowledged at: a
  // version bump with no unsaved edits adopts the new server state; with
  // unsaved edits the draft is kept and a notice offers to load the latest.
  const [sync, setSync] = useState(() => ({
    targetId: target.id,
    version: target.version,
    draft: draftFromTarget(target, timezone),
  }));
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const serverMoved = target.id !== sync.targetId || target.version !== sync.version;
  const dirty = !sameDraft(draft, sync.draft);
  if (serverMoved && (!dirty || target.id !== sync.targetId)) {
    const seeded = draftFromTarget(target, timezone);
    setSync({ targetId: target.id, version: target.version, draft: seeded });
    setDraft(seeded);
    setMessage(null);
    setError(null);
  }
  const platformAccounts = accounts.filter((account) => account.platform === target.platform);
  const account = platformAccounts.find((item) => item.id === target.account_id);
  const stats = captionStats(draft.captionOverride || target.caption);
  const limit = captionLimitFor(draft.postType);
  const overLimit = limit !== null && (limit.kind === "utf16" ? stats.utf16Chars : stats.chars) > limit.max;
  const tiktokIssues = draft.postType === "tiktok_video" ? tiktokScreenIssues(draft.settings) : [];

  const plannedIso = () =>
    draft.date && draft.time ? zonedScheduleIso(draft.date, draft.time, timezone) : null;

  const run = async (name: string, action: () => Promise<PostOut>) => {
    setBusy(name);
    setError(null);
    setMessage(null);
    try {
      const updated = await action();
      onChanged(updated);
      // Acknowledge the new version: the draft is kept (the date/time pickers
      // do not go through save), and the bump is not "changed elsewhere".
      const fresh = findTarget(updated, target.id);
      setSync({
        targetId: target.id,
        version: fresh?.version ?? target.version,
        draft: fresh ? draftFromTarget(fresh, timezone) : sync.draft,
      });
      setMessage("Saved.");
    } catch (err) {
      if (err instanceof V1ApiError && err.status === 409) {
        setError(
          "This post changed elsewhere — the latest is shown; any unsaved edits were kept.",
        );
        onChanged(await V1.posts.get(post.id));
      } else {
        setError(actionError(err, "The change could not be saved."));
      }
    } finally {
      setBusy(null);
    }
  };

  const loadLatest = () => {
    const seeded = draftFromTarget(target, timezone);
    setSync({ targetId: target.id, version: target.version, draft: seeded });
    setDraft(seeded);
    setError(null);
  };

  const save = () =>
    run("save", () =>
      V1.posts.updateTarget(
        post.id,
        target.id,
        {
          account_id: target.account_id,
          post_type: draft.postType,
          caption_override: draft.captionOverride || null,
          settings: draft.settings,
        },
        target.version,
      ),
    );

  const schedule = () =>
    run("schedule", () => {
      const iso = plannedIso();
      if (!iso) throw new V1ApiError("Pick a date and time first.");
      return V1.posts.scheduleTarget(post.id, target.id, { planned_at: iso, now: false });
    });

  const editable = ["draft", "ready", "failed_retryable", "failed_final"].includes(target.state);
  const copy = targetStateCopy(target.state);

  return (
    <article className="panel target-card" data-testid={`target-${target.id}`}>
      <header className="target-card-head">
        <PlatformBadge platform={target.platform as "youtube" | "instagram" | "tiktok"} />
        <strong>{accountLabel(account)}</strong>
        <span className={`status status-${target.state} status-tone-${copy.tone}`}>
          {copy.label}
        </span>
      </header>

      {editable ? (
        <div className="target-edit">
          <div className="two-fields">
            <div className="field">
              <label htmlFor={`tt-type-${target.id}`}>Post type</label>
              <select
                id={`tt-type-${target.id}`}
                value={draft.postType ?? ""}
                onChange={(event) =>
                  setDraft({ ...draft, postType: event.target.value || null, settings: {} })
                }
              >
                <option value="">Choose…</option>
                {postTypesForPlatform(target.platform).map((type) => (
                  <option key={type} value={type}>
                    {postTypeLabel(type)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor={`tt-account-${target.id}`}>Account</label>
              <select
                id={`tt-account-${target.id}`}
                value={target.account_id ?? ""}
                disabled
              >
                <option value="">{accountLabel(account)}</option>
              </select>
              <p className="field-hint">The account is fixed once the target exists.</p>
            </div>
          </div>
          <div className="field">
            <label htmlFor={`tt-caption-${target.id}`}>Caption for this target</label>
            <textarea
              id={`tt-caption-${target.id}`}
              rows={3}
              value={draft.captionOverride}
              placeholder={target.caption}
              onChange={(event) => setDraft({ ...draft, captionOverride: event.target.value })}
            />
            <small className={overLimit ? "text-danger" : undefined}>
              {(draft.captionOverride || target.caption).length}
              {limit ? `/${limit.max}` : ""} chars · {stats.hashtags} hashtags · {stats.mentions} mentions
            </small>
          </div>
          {draft.postType === "tiktok_video" ? (
            <TikTokScreen
              accountId={target.account_id}
              settings={draft.settings}
              onChange={(settings) => setDraft({ ...draft, settings })}
              captionLength={stats.utf16Chars}
              aiGenerated={post.ai_generated}
            />
          ) : (
            <TargetSettingsPanel
              postType={draft.postType}
              accountId={target.account_id}
              platform={target.platform}
              settings={draft.settings}
              onChange={(settings) => setDraft({ ...draft, settings })}
              captionLength={stats.chars}
              media={post.media}
            />
          )}
          <div className="two-fields">
            <div className="field">
              <label htmlFor={`tt-date-${target.id}`}>Publish date</label>
              <input
                id={`tt-date-${target.id}`}
                type="date"
                value={draft.date}
                onChange={(event) => setDraft({ ...draft, date: event.target.value })}
              />
            </div>
            <div className="field">
              <label htmlFor={`tt-time-${target.id}`}>Time ({timezone})</label>
              <input
                id={`tt-time-${target.id}`}
                type="time"
                value={draft.time}
                onChange={(event) => setDraft({ ...draft, time: event.target.value })}
              />
            </div>
          </div>
          <IssueList issues={target.issues} />
          {tiktokIssues.length ? <IssueList issues={tiktokIssues.map((message) => ({ severity: "error", code: "tiktok", message }))} /> : null}
          {serverMoved && dirty ? (
            <p className="notice warning-notice" role="status">
              This target changed; reload to see the latest.{" "}
              <button type="button" className="text-link" onClick={loadLatest}>
                Load the latest
              </button>
            </p>
          ) : null}
          {error ? <p className="dialog-error" role="alert">{error}</p> : null}
          {message ? <p className="form-message form-message-success" role="status">{message}</p> : null}
          <div className="form-actions">
            <button type="button" className="button secondary" onClick={save} disabled={busy !== null}>
              {busy === "save" ? "Saving…" : "Save target"}
            </button>
            <button
              type="button"
              className="button secondary"
              onClick={schedule}
              disabled={busy !== null || !draft.date || !draft.time}
            >
              Schedule
            </button>
            <button
              type="button"
              className="button"
              onClick={() => onRelease(target)}
              disabled={busy !== null || !target.releasable || tiktokIssues.length > 0}
            >
              Release…
            </button>
            <button
              type="button"
              className="button secondary"
              onClick={() => run("cancel", () => V1.posts.cancelTarget(post.id, target.id))}
              disabled={busy !== null}
            >
              Cancel target
            </button>
          </div>
        </div>
      ) : (
        <TargetStatus post={post} target={target} onChanged={onChanged} />
      )}
      {editable ? (
        <details className="target-status-details">
          <summary>Delivery status</summary>
          <TargetStatus post={post} target={target} onChanged={onChanged} />
        </details>
      ) : null}
    </article>
  );
}

// ---------------------------------------------------------------------------
// Create-mode draft target

interface NewTarget {
  key: number;
  platform: string;
  accountId: number | null;
  postType: string | null;
  captionOverride: string;
  settings: SettingsMap;
  date: string;
  time: string;
}

function NewTargetCard({
  target,
  accounts,
  timezone,
  aiGenerated,
  onChange,
  onRemove,
}: {
  target: NewTarget;
  accounts: EditorAccount[];
  timezone: string;
  aiGenerated: boolean;
  onChange: (next: NewTarget) => void;
  onRemove: () => void;
}) {
  const platformAccounts = accounts.filter((account) => account.platform === target.platform);
  const stats = captionStats(target.captionOverride);
  const limit = captionLimitFor(target.postType);
  const over = limit !== null && (limit.kind === "utf16" ? stats.utf16Chars : stats.chars) > limit.max;
  const tiktokIssues = target.postType === "tiktok_video" ? tiktokScreenIssues(target.settings) : [];
  return (
    <article className="panel target-card">
      <header className="target-card-head">
        <PlatformBadge platform={target.platform as "youtube" | "instagram" | "tiktok"} />
        <strong>{accountLabel(platformAccounts.find((a) => a.id === target.accountId))}</strong>
        <button type="button" className="text-link" onClick={onRemove}>
          Remove
        </button>
      </header>
      <div className="two-fields">
        <div className="field">
          <label>Account</label>
          <select
            value={target.accountId ?? ""}
            onChange={(event) =>
              onChange({ ...target, accountId: event.target.value ? Number(event.target.value) : null })
            }
          >
            <option value="">Choose an account…</option>
            {platformAccounts.map((account) => (
              <option key={account.id} value={account.id} disabled={account.status !== "connected"}>
                {accountLabel(account)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Post type</label>
          <select
            value={target.postType ?? ""}
            onChange={(event) => onChange({ ...target, postType: event.target.value || null, settings: {} })}
          >
            <option value="">Choose…</option>
            {postTypesForPlatform(target.platform).map((type) => (
              <option key={type} value={type}>
                {postTypeLabel(type)}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="field">
        <label>Caption override (optional)</label>
        <textarea
          rows={3}
          value={target.captionOverride}
          onChange={(event) => onChange({ ...target, captionOverride: event.target.value })}
        />
        <small className={over ? "text-danger" : undefined}>
          {target.captionOverride.length}{limit ? `/${limit.max}` : ""} · {stats.hashtags} hashtags · {stats.mentions} mentions
        </small>
      </div>
      {target.postType === "tiktok_video" ? (
        <TikTokScreen
          accountId={target.accountId}
          settings={target.settings}
          onChange={(settings) => onChange({ ...target, settings })}
          captionLength={stats.utf16Chars}
          aiGenerated={aiGenerated}
        />
      ) : (
        <TargetSettingsPanel
          postType={target.postType}
          accountId={target.accountId}
          platform={target.platform}
          settings={target.settings}
          onChange={(settings) => onChange({ ...target, settings })}
          captionLength={stats.chars}
          media={[]}
        />
      )}
      <div className="two-fields">
        <div className="field">
          <label>Publish date</label>
          <input type="date" value={target.date} onChange={(event) => onChange({ ...target, date: event.target.value })} />
        </div>
        <div className="field">
          <label>Time ({timezone})</label>
          <input type="time" value={target.time} onChange={(event) => onChange({ ...target, time: event.target.value })} />
        </div>
      </div>
      {tiktokIssues.length ? (
        <ul className="issue-list">
          {tiktokIssues.map((issue) => (
            <li key={issue} className="issue-error">{issue}</li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}

// ---------------------------------------------------------------------------
// The editor

export function PostEditor({
  initialPost,
  accounts,
  timezone,
}: {
  initialPost: PostOut | null;
  accounts: EditorAccount[];
  timezone: string;
}) {
  const router = useRouter();
  const [post, setPost] = useState<PostOut | null>(initialPost);
  const [title, setTitle] = useState(initialPost?.title ?? "");
  const [body, setBody] = useState(initialPost?.body ?? "");
  const [notes, setNotes] = useState(initialPost?.notes ?? "");
  const [aiGenerated, setAiGenerated] = useState(initialPost?.ai_generated ?? false);
  const [media, setMedia] = useState<PickedMedia[]>(
    () =>
      initialPost?.media.map((item) => ({
        asset_id: item.asset_id,
        kind: item.kind,
        status: item.status,
        label: `asset ${item.asset_id}`,
      })) ?? [],
  );
  const [newTargets, setNewTargets] = useState<NewTarget[]>([]);
  const [addPlatform, setAddPlatform] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [releaseTarget, setReleaseTarget] = useState<TargetOut | null>(null);
  const platforms = useMemo(
    () => [...new Set(accounts.map((account) => account.platform))],
    [accounts],
  );

  const refetch = useCallback(async () => {
    if (post === null) return;
    try {
      setPost(await V1.posts.get(post.id));
    } catch {
      // A failed refresh keeps the current view; SSE/60s poll retries.
    }
  }, [post]);

  const { mode: streamMode } = useEventStream({
    enabled: post !== null,
    onEvent: (event) => {
      if (POST_EVENT_TYPES.has(event.type)) void refetch();
    },
    onResync: () => void refetch(),
    onPoll: () => void refetch(),
  });

  const onChanged = useCallback((updated: PostOut) => {
    setPost(updated);
  }, []);

  const createPost = async () => {
    setBusy("create");
    setError(null);
    try {
      const created = await V1.posts.create({
        title: title.trim() || "Untitled post",
        body,
        notes: notes || null,
        ai_generated: aiGenerated,
        media_ids: media.map((item) => item.asset_id),
        targets: newTargets
          .filter((target) => target.accountId !== null && target.postType !== null)
          .map((target) => ({
            platform: target.platform as "youtube" | "instagram" | "tiktok",
            account_id: target.accountId,
            post_type: target.postType,
            caption_override: target.captionOverride || null,
            settings: target.settings,
            planned_at:
              target.date && target.time
                ? zonedScheduleIso(target.date, target.time, timezone)
                : null,
          })),
      });
      router.push(`/posts/${created.id}`);
    } catch (err) {
      setError(actionError(err, "The post could not be created."));
      setBusy(null);
    }
  };

  const savePost = async () => {
    if (post === null) return;
    setBusy("save");
    setError(null);
    try {
      const updated = await V1.posts.patch(
        post.id,
        {
          title: title.trim() || post.title,
          body,
          notes: notes || null,
          ai_generated: aiGenerated,
          media_ids: media.map((item) => item.asset_id),
          clear_schedule: false,
        },
        post.version,
      );
      setPost(updated);
    } catch (err) {
      if (err instanceof V1ApiError && err.status === 409) {
        setError("This post changed elsewhere — reloaded the latest version.");
        setPost(await V1.posts.get(post.id));
      } else {
        setError(actionError(err, "The post could not be saved."));
      }
    } finally {
      setBusy(null);
    }
  };

  const releaseTargetAccount = releaseTarget
    ? accountLabel(accounts.find((account) => account.id === releaseTarget.account_id))
    : "";

  return (
    <div className="post-editor">
      <header className="page-header">
        <p className="eyebrow">
          <Link className="back-link" href="/posts">Posts</Link> / {post ? `post ${post.id}` : "new"}
        </p>
        <h1>{post ? (post.title || "Untitled post") : "New post"}</h1>
        {post ? (
          <p className="lede">
            {post.status} · {post.targets.length} target{post.targets.length === 1 ? "" : "s"}
            {streamMode === "polling" ? " · checking every minute" : ""}
          </p>
        ) : (
          <p className="lede">Compose once; set each account&apos;s own format and time.</p>
        )}
      </header>

      <section className="panel">
        <div className="field">
          <label htmlFor="post-title">Title</label>
          <input id="post-title" type="text" value={title} onChange={(event) => setTitle(event.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="post-body">Base caption</label>
          <textarea id="post-body" rows={4} value={body} onChange={(event) => setBody(event.target.value)} />
          <small>{captionStats(body).chars} chars · {captionStats(body).hashtags} hashtags · {captionStats(body).mentions} mentions</small>
        </div>
        <div className="field">
          <label htmlFor="post-notes">Internal notes (never published)</label>
          <input id="post-notes" type="text" value={notes} onChange={(event) => setNotes(event.target.value)} />
        </div>
        <label className="switch-label">
          <input type="checkbox" checked={aiGenerated} onChange={(event) => setAiGenerated(event.target.checked)} />
          An agent drafted this post
        </label>
        <h3 className="section-heading">Media</h3>
        <MediaPicker media={media} onAdded={(item) => setMedia((current) => [...current, item])} />
        {post !== null ? (
          <div className="form-actions">
            <button type="button" className="button secondary" onClick={savePost} disabled={busy !== null}>
              {busy === "save" ? "Saving…" : "Save post"}
            </button>
          </div>
        ) : null}
      </section>

      {post !== null ? (
        <>
          <section aria-label="Targets">
            <h2 className="section-heading">Targets</h2>
            {post.targets.map((target) => (
              <TargetCard
                key={target.id}
                post={post}
                target={target}
                accounts={accounts}
                timezone={timezone}
                onChanged={onChanged}
                onRelease={setReleaseTarget}
              />
            ))}
          </section>
          <section className="panel">
            <h3 className="section-heading">Add a target</h3>
            <AddTargetForm
              post={post}
              accounts={accounts}
              platforms={platforms}
              onChanged={onChanged}
            />
          </section>
        </>
      ) : (
        <>
          <section aria-label="Targets">
            <h2 className="section-heading">Targets</h2>
            {newTargets.map((target) => (
              <NewTargetCard
                key={target.key}
                target={target}
                accounts={accounts}
                timezone={timezone}
                aiGenerated={aiGenerated}
                onChange={(next) =>
                  setNewTargets((current) => current.map((item) => (item.key === target.key ? next : item)))
                }
                onRemove={() => setNewTargets((current) => current.filter((item) => item.key !== target.key))}
              />
            ))}
            <div className="field">
              <label htmlFor="new-target-platform">Add a target for</label>
              <select
                id="new-target-platform"
                value={addPlatform}
                onChange={(event) => {
                  const platform = event.target.value;
                  setAddPlatform("");
                  if (!platform) return;
                  setNewTargets((current) => [
                    ...current,
                    {
                      key: Date.now() + current.length,
                      platform,
                      accountId: null,
                      postType: null,
                      captionOverride: "",
                      settings: {},
                      date: "",
                      time: "",
                    },
                  ]);
                }}
              >
                <option value="">Choose a platform…</option>
                {platforms.map((platform) => (
                  <option key={platform} value={platform}>
                    {platformLabel(platform)}
                  </option>
                ))}
              </select>
            </div>
            {newTargets.length === 0 ? (
              <p className="field-hint">Add at least one account to publish to.</p>
            ) : null}
          </section>
          {error ? <p className="dialog-error" role="alert">{error}</p> : null}
          <div className="form-actions">
            <button
              type="button"
              className="button"
              onClick={createPost}
              disabled={busy !== null || !title.trim() || newTargets.every((t) => t.accountId === null || t.postType === null)}
            >
              {busy === "create" ? "Creating…" : "Create post"}
            </button>
          </div>
        </>
      )}
      {error && post !== null ? <p className="dialog-error" role="alert">{error}</p> : null}
      {releaseTarget && post ? (
        <ReleaseDialog
          post={post}
          target={releaseTarget}
          accountLabel={releaseTargetAccount}
          onClose={() => setReleaseTarget(null)}
          onReleased={(updated) => {
            setPost(updated);
            setReleaseTarget(null);
          }}
        />
      ) : null}
    </div>
  );
}

function AddTargetForm({
  post,
  accounts,
  platforms,
  onChanged,
}: {
  post: PostOut;
  accounts: EditorAccount[];
  platforms: string[];
  onChanged: (post: PostOut) => void;
}) {
  const [platform, setPlatform] = useState("");
  const [accountId, setAccountId] = useState<number | null>(null);
  const [postType, setPostType] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const platformAccounts = accounts.filter((account) => account.platform === platform);

  const add = async () => {
    if (!platform || accountId === null) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await V1.posts.addTarget(post.id, {
        platform: platform as "youtube" | "instagram" | "tiktok",
        account_id: accountId,
        post_type: postType || null,
      });
      onChanged(updated);
      setPlatform("");
      setAccountId(null);
      setPostType("");
    } catch (err) {
      setError(actionError(err, "The target could not be added."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="add-target">
      <div className="two-fields">
        <div className="field">
          <label htmlFor="add-platform">Platform</label>
          <select
            id="add-platform"
            value={platform}
            onChange={(event) => {
              setPlatform(event.target.value);
              setAccountId(null);
              setPostType("");
            }}
          >
            <option value="">Choose…</option>
            {platforms.map((item) => (
              <option key={item} value={item}>{platformLabel(item)}</option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="add-account">Account</label>
          <select
            id="add-account"
            value={accountId ?? ""}
            onChange={(event) => setAccountId(event.target.value ? Number(event.target.value) : null)}
          >
            <option value="">Choose…</option>
            {platformAccounts.map((account) => (
              <option key={account.id} value={account.id} disabled={account.status !== "connected"}>
                {accountLabel(account)}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="field">
        <label htmlFor="add-type">Post type</label>
        <select id="add-type" value={postType} onChange={(event) => setPostType(event.target.value)}>
          <option value="">Decide in the editor</option>
          {postTypesForPlatform(platform).map((type) => (
            <option key={type} value={type}>{postTypeLabel(type)}</option>
          ))}
        </select>
      </div>
      {error ? <p className="dialog-error" role="alert">{error}</p> : null}
      <button
        type="button"
        className="button secondary"
        onClick={add}
        disabled={busy || !platform || accountId === null}
      >
        {busy ? "Adding…" : "Add target"}
      </button>
    </div>
  );
}
