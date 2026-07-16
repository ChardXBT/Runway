"use client";

import { useState } from "react";

import { API_URL } from "@/lib/api";

export function GenerateButton() {
  const [status, setStatus] = useState("");
  async function generate() {
    setStatus("Generating fixture batch…");
    const response = await fetch(`${API_URL}/api/generation-runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days: 10 }),
    });
    const result = await response.json();
    setStatus(response.ok ? `Queue ready: ${result.proposal_ids.length} days.` : result.detail);
  }
  return <div className="generate-control"><button className="button" onClick={generate}>Generate batch</button><small role="status">{status}</small></div>;
}
