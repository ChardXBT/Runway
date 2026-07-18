"use client";

import { useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import { actionError, readApiJson } from "@/lib/client-api";
import { isRecord } from "@/lib/guards";

export function EligibilityToggle({ postId, initial }: { postId: number; initial: boolean }) {
  const [eligible, setEligible] = useState(initial);
  const [message, setMessage] = useState("");
  const [messageIsError, setMessageIsError] = useState(false);
  const [busy, setBusy] = useState(false);
  const actionLock = useRef(false);

  async function toggle() {
    if (actionLock.current) return;
    const next = !eligible;
    actionLock.current = true;
    setBusy(true);
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
  return (
    <div className="eligibility-toggle">
      <button
        type="button"
        className="button secondary"
        onClick={toggle}
        disabled={busy}
        aria-busy={busy}
      >
        {busy
          ? "Saving eligibility…"
          : eligible
            ? "Exclude from profile"
            : "Include in profile"}
      </button>
      <small role={messageIsError ? "alert" : "status"}>{message}</small>
    </div>
  );
}
