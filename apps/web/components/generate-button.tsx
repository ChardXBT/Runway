"use client";

import { useState } from "react";

import { API_URL } from "@/lib/api";

export function GenerateButton() {
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  async function generate() {
    setBusy(true);
    setStatus("Building the ten-day draft…");
    try {
      const response = await fetch(`${API_URL}/api/generation-runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days: 10 }),
      });
      const result = await response.json();
      setStatus(
        response.ok
          ? `${result.proposal_ids.length} proposal${result.proposal_ids.length === 1 ? "" : "s"} ready.`
          : result.detail,
      );
    } catch {
      setStatus("LeeWay could not reach the local service.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="generate-control">
      <button className="button" disabled={busy} onClick={generate}>
        {busy ? "Building draft…" : "Build ten-day draft"}
      </button>
      <small role="status">{status}</small>
    </div>
  );
}
