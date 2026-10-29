"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { ContentThumb } from "@/components/content-thumb";
import { PlatformBadge, platformLabel } from "@/components/platform-badge";
import { StatePanel } from "@/components/state-panel";
import { API_URL } from "@/lib/api";
import { ApiError, actionError, readApiJson } from "@/lib/client-api";
import { excerpt, formatHandle, formatRelativeTime } from "@/lib/format";
import {
  isInboxDetail,
  isInboxListResponse,
  isReplyActionResult,
} from "@/lib/guards";
import type {
  AccountView,
  InboxComment,
  InboxDetail,
  InboxListResponse,
  PlatformKind,
} from "@/lib/types";

const STATUSES = ["", "new", "read", "answered", "escalated", "ignored"];
const PAGE_SIZE = 50;

function statusLabel(status: string) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

export function InboxWorkspace({
  accounts,
  list,
  filters,
  selectedId,
}: {
  accounts: AccountView[];
  list: InboxListResponse;
  filters: {
    platform: string;
    account: string;
    status: string;
    unanswered: boolean;
    q: string;
  };
  selectedId: number | null;
}) {
  const router = useRouter();
  const [extraItems, setExtraItems] = useState<InboxComment[]>([]);
  const [overrides, setOverrides] = useState<
    Record<number, { status: string; has_owner_reply: boolean }>
  >({});
  const [loadingMore, setLoadingMore] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [detail, setDetail] = useState<InboxDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailFor, setDetailFor] = useState<number | null>(selectedId);
  const [qInput, setQInput] = useState(filters.q);
  const [prevList, setPrevList] = useState(list);
  const [prevQ, setPrevQ] = useState(filters.q);
  const currentList = useRef(list);

  useEffect(() => {
    currentList.current = list;
  }, [list]);

  if (prevList !== list) {
    setPrevList(list);
    setExtraItems([]);
    setOverrides({});
    setListError(null);
  }
  if (prevQ !== filters.q) {
    setPrevQ(filters.q);
    setQInput(filters.q);
  }
  if (detailFor !== selectedId) {
    setDetailFor(selectedId);
    setDetail(null);
    setDetailError(null);
  }

  const items = useMemo(
    () =>
      [...list.items, ...extraItems].map((comment) => {
        const override = overrides[comment.id];
        return override === undefined ? comment : { ...comment, ...override };
      }),
    [list.items, extraItems, overrides],
  );
  const total = list.total;

  const accountById = useMemo(
    () => new Map(accounts.map((account) => [account.id, account])),
    [accounts],
  );

  useEffect(() => {
    if (selectedId === null) return;
    let cancelled = false;
    fetch(`${API_URL}/api/inbox/${selectedId}`, { cache: "no-store" })
      .then((response) =>
        readApiJson(response, {
          validate: isInboxDetail,
          failureMessage: "Could not load the comment.",
        }),
      )
      .then((payload) => {
        if (!cancelled) setDetail(payload);
      })
      .catch((err) => {
        if (!cancelled) {
          setDetailError(actionError(err, "Could not load the comment."));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const buildQuery = (overrides: Record<string, string>) => {
    const query = new URLSearchParams();
    const merged: Record<string, string> = {
      ...filters,
      unanswered: filters.unanswered ? "1" : "0",
      ...overrides,
    };
    if (merged.platform) query.set("platform", merged.platform);
    if (merged.account) query.set("account", merged.account);
    if (merged.status) query.set("status", merged.status);
    query.set("unanswered", merged.unanswered === "0" ? "0" : "1");
    if (merged.q) query.set("q", merged.q);
    if (merged.id) query.set("id", merged.id);
    return `/inbox?${query.toString()}`;
  };

  const loadMore = async () => {
    setLoadingMore(true);
    setListError(null);
    const requestedList = currentList.current;
    try {
      const query = new URLSearchParams();
      if (filters.platform) query.set("platform", filters.platform);
      if (filters.account) query.set("account_id", filters.account);
      if (filters.status) query.set("status", filters.status);
      if (filters.unanswered) query.set("unanswered", "true");
      if (filters.q) query.set("q", filters.q);
      query.set("limit", String(PAGE_SIZE));
      query.set("offset", String(items.length));
      const response = await fetch(`${API_URL}/api/inbox?${query}`, {
        cache: "no-store",
      });
      const payload = await readApiJson(response, {
        validate: isInboxListResponse,
        failureMessage: "Could not load more comments.",
      });
      if (currentList.current !== requestedList) return;
      setExtraItems((current) => [...current, ...payload.items]);
    } catch (err) {
      setListError(actionError(err, "Could not load more comments."));
    } finally {
      setLoadingMore(false);
    }
  };

  const listColumn = (
    <section className="inbox-list panel" aria-label="Comments">
      <div className="section-heading">
        <h2>
          {filters.unanswered ? "Unanswered" : "Comments"}
          <span className="panel-hint"> · {total} total</span>
        </h2>
      </div>
      {items.length === 0 ? (
        <StatePanel
          variant="empty"
          title={filters.unanswered ? "All caught up" : "No comments match"}
          body={
            filters.unanswered
              ? "No unanswered comments match these filters. Turn off the unanswered filter to see everything."
              : "Adjust the filters or sync an account to import comments."
          }
        />
      ) : (
        <ul className="inbox-rows">
          {items.map((comment) => {
            const isSelected = comment.id === selectedId;
            return (
              <li key={comment.id}>
                <Link
                  href={buildQuery({ id: String(comment.id) })}
                  scroll={false}
                  className={`inbox-row${isSelected ? " selected" : ""}`}
                  aria-current={isSelected ? "true" : undefined}
                >
                  <div className="inbox-row-head">
                    <PlatformBadge platform={comment.platform} size="sm" />
                    <span className="inbox-author">
                      {comment.author_name ?? "Unknown author"}
                    </span>
                    <time
                      suppressHydrationWarning
                      dateTime={comment.published_at ?? undefined}
                      title={comment.published_at ?? undefined}
                    >
                      {formatRelativeTime(comment.published_at)}
                    </time>
                  </div>
                  <p className="inbox-excerpt">
                    {excerpt(comment.text, 140) ?? "(no text)"}
                  </p>
                  <div className="inbox-row-chips">
                    <span className={`status status-${comment.status}`}>
                      {statusLabel(comment.status)}
                    </span>
                    {comment.has_owner_reply ? (
                      <span className="status">Replied</span>
                    ) : null}
                    {comment.draft !== null ? (
                      <span className="status status-tone-warning">Draft</span>
                    ) : null}
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
      {items.length < total ? (
        <div className="inbox-load-more">
          <button
            type="button"
            className="button secondary"
            onClick={loadMore}
            disabled={loadingMore}
          >
            {loadingMore ? "Loading…" : `Load more (${total - items.length} left)`}
          </button>
        </div>
      ) : null}
      {listError !== null ? (
        <p className="notice error-notice" role="alert">
          {listError}
        </p>
      ) : null}
    </section>
  );

  return (
    <div className="inbox-workspace">
      <header className="page-header">
        <div className="page-header-copy">
          <p className="eyebrow">Audience</p>
          <h1>Inbox</h1>
        </div>
        <div className="conveyor-stats">
          <span>
            <strong>{list.counts.unanswered}</strong> unanswered
          </span>
          <span>
            <strong>{list.counts.new}</strong> new
          </span>
          <span>
            <strong>{list.total}</strong> shown
          </span>
        </div>
      </header>

      <form
        className="inbox-filters"
        onSubmit={(event) => {
          event.preventDefault();
          router.push(buildQuery({ q: qInput, id: "" }));
        }}
      >
        <label>
          Platform
          <select
            value={filters.platform}
            onChange={(event) =>
              router.push(buildQuery({ platform: event.target.value, account: "", id: "" }))
            }
          >
            <option value="">All platforms</option>
            {(["youtube", "instagram", "tiktok"] as PlatformKind[]).map(
              (platform) => (
                <option key={platform} value={platform}>
                  {platformLabel(platform)}
                </option>
              ),
            )}
          </select>
        </label>
        <label>
          Account
          <select
            value={filters.account}
            onChange={(event) =>
              router.push(buildQuery({ account: event.target.value, id: "" }))
            }
          >
            <option value="">All accounts</option>
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {formatHandle(account.handle) ||
                  account.display_name ||
                  `account ${account.id}`}
              </option>
            ))}
          </select>
        </label>
        <label>
          Status
          <select
            value={filters.status}
            onChange={(event) =>
              router.push(buildQuery({ status: event.target.value, id: "" }))
            }
          >
            {STATUSES.map((status) => (
              <option key={status || "all"} value={status}>
                {status === "" ? "All statuses" : statusLabel(status)}
              </option>
            ))}
          </select>
        </label>
        <label className="switch-label">
          <input
            type="checkbox"
            role="switch"
            checked={filters.unanswered}
            onChange={(event) =>
              router.push(
                buildQuery({
                  unanswered: event.target.checked ? "1" : "0",
                  id: "",
                }),
              )
            }
          />
          Unanswered only
        </label>
        <label className="inbox-search">
          Search
          <input
            type="search"
            value={qInput}
            placeholder="Text or author"
            onChange={(event) => setQInput(event.target.value)}
          />
        </label>
        <button type="submit" className="button">
          Search
        </button>
      </form>

      <div className="inbox-body">
        {selectedId !== null ? (
          <div className="inbox-detail-col">
            <Link href={buildQuery({ id: "" })} className="back-link">
              ← Back to list
            </Link>
            {detailError !== null ? (
              <StatePanel
                variant="error"
                title="Comment could not be loaded"
                body={detailError}
              />
            ) : detail === null ? (
              <StatePanel variant="loading" title="Loading comment" />
            ) : (
              <CommentDetail
                key={detail.comment.id}
                detail={detail}
                account={accountById.get(detail.comment.account_id)}
                onStatusChange={(status) =>
                  setOverrides((current) => ({
                    ...current,
                    [detail.comment.id]: {
                      status,
                      has_owner_reply:
                        status === "answered"
                          ? true
                          : detail.comment.has_owner_reply,
                    },
                  }))
                }
                onDetailChange={setDetail}
              />
            )}
          </div>
        ) : null}
        {listColumn}
      </div>
    </div>
  );
}

function CommentDetail({
  detail,
  account,
  onStatusChange,
  onDetailChange,
}: {
  detail: InboxDetail;
  account: AccountView | undefined;
  onStatusChange: (status: string) => void;
  onDetailChange: (detail: InboxDetail) => void;
}) {
  const comment = detail.comment;
  const [statusError, setStatusError] = useState<string | null>(null);
  const [draftText, setDraftText] = useState(detail.draft ?? "");
  const [draftState, setDraftState] = useState<"idle" | "saving" | "saved" | "error">(
    "idle",
  );
  const [confirmSend, setConfirmSend] = useState(false);
  const [sending, setSending] = useState(false);
  const [replyOutcome, setReplyOutcome] = useState<{
    tone: "success" | "warning" | "error";
    message: string;
  } | null>(null);
  const [savedDraft, setSavedDraft] = useState(detail.draft ?? "");

  const platform = comment.platform;
  const platformName = platformLabel(platform);

  const patchStatus = async (status: string) => {
    const previous = comment.status;
    onStatusChange(status);
    setStatusError(null);
    try {
      const response = await fetch(`${API_URL}/api/inbox/${comment.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new ApiError(
          typeof payload?.detail === "string"
            ? payload.detail
            : "Could not update the status.",
        );
      }
      onDetailChange({
        ...detail,
        comment: { ...comment, status },
      });
    } catch (err) {
      onStatusChange(previous);
      setStatusError(actionError(err, "Could not update the status."));
    }
  };

  const saveDraft = async () => {
    const text = draftText;
    if (text === savedDraft) return;
    setDraftState("saving");
    try {
      const response = await fetch(`${API_URL}/api/inbox/${comment.id}/draft`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!response.ok) throw new ApiError("Could not save the draft.");
      setSavedDraft(text);
      setDraftState("saved");
      onDetailChange({ ...detail, draft: text });
    } catch {
      setDraftState("error");
    }
  };

  const clearDraft = async () => {
    setDraftState("saving");
    try {
      const response = await fetch(`${API_URL}/api/inbox/${comment.id}/draft`, {
        method: "DELETE",
      });
      if (!response.ok) throw new ApiError("Could not clear the draft.");
      setSavedDraft("");
      setDraftText("");
      setDraftState("saved");
      onDetailChange({ ...detail, draft: null });
    } catch {
      setDraftState("error");
    }
  };

  const sendReply = async () => {
    setSending(true);
    setReplyOutcome(null);
    try {
      const response = await fetch(`${API_URL}/api/inbox/${comment.id}/reply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: draftText, confirmed: true }),
      });
      const action = await readApiJson(response, {
        validate: isReplyActionResult,
        failureMessage: `Runway could not post the reply to ${platformName}.`,
      });
      if (action.state === "completed" && action.verified) {
        setReplyOutcome({
          tone: "success",
          message: "Reply posted and verified.",
        });
        onDetailChange({
          ...detail,
          comment: { ...comment, status: "answered", has_owner_reply: true },
        });
        onStatusChange("answered");
      } else {
        setReplyOutcome({
          tone: "warning",
          message: `Runway could not confirm the reply. Check on ${platformName} before retrying.`,
        });
      }
      setConfirmSend(false);
    } catch (err) {
      setReplyOutcome({
        tone: "error",
        message: actionError(
          err,
          `Runway could not post the reply to ${platformName}.`,
        ),
      });
      setConfirmSend(false);
    } finally {
      setSending(false);
    }
  };

  const capability = detail.reply_capability;

  return (
    <section className="inbox-detail panel" aria-label="Comment detail">
      <div className="section-heading inbox-detail-head">
        <h2>
          <PlatformBadge platform={platform} size="sm" />{" "}
          {comment.author_name ?? "Unknown author"}
        </h2>
        <div className="inbox-detail-meta">
          <span className={`status status-${comment.status}`}>
            {statusLabel(comment.status)}
          </span>
          {comment.like_count !== null ? (
            <span className="status">{comment.like_count} likes</span>
          ) : null}
          {comment.permalink !== null ? (
            <a href={comment.permalink} target="_blank" rel="noreferrer">
              Open on {platformName}
            </a>
          ) : null}
        </div>
      </div>
      <p className="inbox-comment-text">{comment.text}</p>
      <p className="inbox-comment-time" suppressHydrationWarning>
        {formatRelativeTime(comment.published_at)}
        {account !== undefined
          ? ` · on ${formatHandle(account.handle) || account.display_name || "account"}`
          : ""}
      </p>

      {detail.content !== null ? (
        <div className="inbox-content-context">
          <ContentThumb src={detail.content.thumbnail_url} platform={detail.content.platform} size={48} />
          <div>
            <Link href={`/content/${detail.content.id}`}>
              {detail.content.title ??
                excerpt(detail.content.caption, 60) ??
                "(no caption)"}
            </Link>
            <span className="inbox-content-type">
              {detail.content.content_type}
            </span>
          </div>
        </div>
      ) : null}

      {detail.replies.length > 0 ? (
        <ul className="inbox-replies">
          {detail.replies.map((reply) => (
            <li
              key={reply.id}
              className={reply.author_is_owner ? "owner-reply" : ""}
            >
              <strong>
                {reply.author_name ?? "Unknown"}
                {reply.author_is_owner ? " (you)" : ""}
              </strong>
              <span className="inbox-reply-time" suppressHydrationWarning>
                {formatRelativeTime(reply.published_at)}
              </span>
              <p>{reply.text}</p>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="inbox-status-actions" role="group" aria-label="Status">
        {(
          [
            ["read", "Mark read"],
            ["escalated", "Escalate"],
            ["ignored", "Ignore"],
            ["answered", "Mark answered"],
          ] as const
        )
          .filter(([status]) => status !== comment.status)
          .map(([status, label]) => (
            <button
              key={status}
              type="button"
              className="button secondary"
              onClick={() => patchStatus(status)}
            >
              {label}
            </button>
          ))}
      </div>
      {statusError !== null ? (
        <p className="notice error-notice" role="alert">
          {statusError}
        </p>
      ) : null}

      <div className="inbox-reply-box">
        <label>
          Reply draft
          <textarea
            value={draftText}
            rows={3}
            onChange={(event) => {
              setDraftText(event.target.value);
              setDraftState("idle");
            }}
            onBlur={saveDraft}
            placeholder={`Draft a reply to post on ${platformName}…`}
          />
        </label>
        <div className="inbox-reply-meta">
          <span role="status">
            {draftState === "saving"
              ? "Saving…"
              : draftState === "saved"
                ? "Saved"
                : draftState === "error"
                  ? "Could not save the draft."
                  : ""}
          </span>
          {draftText !== "" || savedDraft !== "" ? (
            <button
              type="button"
              className="button secondary"
              onClick={clearDraft}
              disabled={draftState === "saving"}
            >
              Clear
            </button>
          ) : null}
        </div>
      </div>

      <div className="inbox-send">
        {capability.ready ? (
          confirmSend ? (
            <div className="inbox-send-confirm" role="group" aria-label="Confirm send">
              <button
                type="button"
                className="button"
                onClick={sendReply}
                disabled={sending || draftText.trim() === ""}
              >
                {sending ? "Sending…" : `Confirm send to ${platformName}`}
              </button>
              <button
                type="button"
                className="button secondary"
                onClick={() => setConfirmSend(false)}
                disabled={sending}
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="button"
              onClick={() => setConfirmSend(true)}
              disabled={draftText.trim() === ""}
            >
              Send reply
            </button>
          )
        ) : (
          <>
            <button
              type="button"
              className="button"
              disabled
              title={capability.detail}
            >
              Send reply
            </button>
            <p className="inbox-send-detail">{capability.detail}</p>
          </>
        )}
        {capability.handoff_url !== null ? (
          <a
            href={capability.handoff_url}
            target="_blank"
            rel="noreferrer"
            className="inbox-handoff"
          >
            Reply on {platformName} instead
          </a>
        ) : null}
      </div>
      {replyOutcome !== null ? (
        <p
          className={`notice ${
            replyOutcome.tone === "success"
              ? "success-notice"
              : replyOutcome.tone === "warning"
                ? "warning-notice"
                : "error-notice"
          }`}
          role={replyOutcome.tone === "error" ? "alert" : "status"}
        >
          {replyOutcome.message}
        </p>
      ) : null}
    </section>
  );
}
