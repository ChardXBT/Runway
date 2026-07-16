"use client";

import { useState } from "react";

import { API_URL } from "@/lib/api";

export function EligibilityToggle({ postId, initial }: { postId: number; initial: boolean }) {
  const [eligible, setEligible] = useState(initial);
  const [message, setMessage] = useState("");
  async function toggle() {
    const next = !eligible;
    const response = await fetch(`${API_URL}/api/catalog/${postId}/eligibility`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_training_eligible: next }),
    });
    if (response.ok) { setEligible(next); setMessage("Saved."); } else setMessage("Could not update eligibility.");
  }
  return <div className="eligibility-toggle"><button className="button secondary" onClick={toggle}>{eligible ? "Exclude from profile" : "Include in profile"}</button><small role="status">{message}</small></div>;
}
