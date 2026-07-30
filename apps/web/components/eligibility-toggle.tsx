"use client";

import { useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import {
  actionError,
  hasUncertainOutcome,
  readApiJson,
} from "@/lib/client-api";
import { isRecord } from "@/lib/guards";

export function EligibilityToggle({ postId, initial }: { postId: number; initial: boolean }) {
  const [eligible, setEligible] = useState(initial);
  const [message, setMessage] = useState("");
  const [messageIsError, setMessageIsError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const actionLock = useRef(false);
  const pendingValue = useRef<boolean | null>(null);

  async function toggle() {
    if (actionLock.current) return;
    const next = !eligible;
    pendingValue.current = next;
    actionLock.current = true;
    setBusy(true);
    setUncertain(false);
    setMessage("Saving eligibility…");
    setMessageIsError(false);
    try {
      const response = await fetch(`${API_URL}/api/catalog/${postId}/eligibility`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_training_eligible: next }),
      });
      await readApiJson(response, {
        validate: (value): value is Record<string, unknown> =>
          isRecord(value) && value.is_training_eligible === next,
        failureMessage: "Could not update eligibility.",
      });
      setEligible(next);
      setMessage("Eligibility saved.");
    } catch (error) {
      setUncertain(hasUncertainOutcome(error));
      setMessage(
        actionError(
          error,
          "Could not update eligibility. The current setting is unchanged.",
          "The eligibility response could not be verified. The displayed setting is unchanged; reload before trying again.",
        ),
      );
      setMessageIsError(true);
    } finally {
      actionLock.current = false;
      setBusy(false);
    }
  }

  async function reconcile() {
    if (actionLock.current || pendingValue.current === null) return;
    actionLock.current = true;
    setBusy(true);
    setMessage("Checking saved eligibility…");
    setMessageIsError(false);
    try {
      const response = await fetch(`${API_URL}/api/catalog/${postId}`, {
        cache: "no-store",
      });
      const saved = await readApiJson(response, {
        validate: (value): value is Record<string, unknown> =>
          isRecord(value) && typeof value.is_training_eligible === "boolean",
        failureMessage: "Saved eligibility could not be checked.",
        malformedMessage:
          "The eligibility check returned unreadable data. The displayed setting has not been changed.",
      });
      const savedValue = saved.is_training_eligible as boolean;
      setEligible(savedValue);
      setUncertain(false);
      if (savedValue === pendingValue.current) {
        setMessage("Eligibility save verified.");
        setMessageIsError(false);
      } else {
        setMessage("The change did not take effect. You can try it again.");
        setMessageIsError(true);
      }
    } catch (error) {
      setMessage(
        actionError(
          error,
          "Saved eligibility could not be checked. The displayed setting has not been changed.",
        ),
      );
      setMessageIsError(true);
    } finally {
      actionLock.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="eligibility-toggle">
      <button
        type="button"
        className="button secondary"
        onClick={toggle}
        disabled={busy || uncertain}
        aria-busy={busy}
      >
        {busy
          ? "Saving eligibility…"
          : eligible
            ? "Exclude from profile"
            : "Include in profile"}
      </button>
      {uncertain && (
        <button
          type="button"
          className="text-link"
          onClick={reconcile}
          disabled={busy}
        >
          Check saved state
        </button>
      )}
      <small role={messageIsError ? "alert" : "status"}>{message}</small>
    </div>
  );
}
