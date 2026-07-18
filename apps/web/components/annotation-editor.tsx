"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import { actionError, readApiJson } from "@/lib/client-api";
import { isRecord } from "@/lib/guards";

type Annotation = {
  effective: {
    franchise?: string | null;
    visible_characters?: string[];
    scene_description?: string;
    composition?: string;
    tone?: string;
  };
  review_status: string;
};

function isAnnotation(value: unknown): value is Annotation {
  if (!isRecord(value) || !isRecord(value.effective)) return false;
  const effective = value.effective;
  return (
    (effective.franchise === undefined ||
      effective.franchise === null ||
      typeof effective.franchise === "string") &&
    (effective.visible_characters === undefined ||
      (Array.isArray(effective.visible_characters) &&
        effective.visible_characters.every(
          (character) => typeof character === "string",
        ))) &&
    (effective.scene_description === undefined ||
      typeof effective.scene_description === "string") &&
    (effective.composition === undefined ||
      typeof effective.composition === "string") &&
    (effective.tone === undefined || typeof effective.tone === "string") &&
    typeof value.review_status === "string"
  );
}

export function AnnotationEditor({ postId }: { postId: number }) {
  const [annotation, setAnnotation] = useState<Annotation | null>(null);
  const [status, setStatus] = useState("Loading annotation…");
  const [statusIsError, setStatusIsError] = useState(false);
  const [saving, setSaving] = useState(false);
  const actionLock = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch(`${API_URL}/api/annotations/${postId}`, {
          signal: controller.signal,
        });
        const value = await readApiJson(response, {
          validate: isAnnotation,
          failureMessage: "Run historical analysis to create this annotation.",
          malformedMessage:
            "The annotation response was unreadable. Reload before editing this record.",
        });
        setAnnotation(value);
        setStatus("");
        setStatusIsError(false);
      } catch (error) {
        if (controller.signal.aborted) return;
        setStatus(
          actionError(
            error,
            "Run historical analysis to create this annotation.",
          ),
        );
        setStatusIsError(true);
      }
    }
    void load();
    return () => controller.abort();
  }, [postId]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (actionLock.current) return;
    const data = new FormData(event.currentTarget);
    const fields = {
      franchise: String(data.get("franchise") || "") || null,
      characters: String(data.get("characters") || "")
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean),
      scene_description: String(data.get("scene_description") || ""),
      composition: String(data.get("composition") || ""),
      tone: String(data.get("tone") || ""),
    };
    actionLock.current = true;
    setSaving(true);
    setStatus("Saving…");
    setStatusIsError(false);
    try {
      const response = await fetch(`${API_URL}/api/annotations/${postId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          fields,
          review_note: "Reviewed in the local catalogue annotation editor.",
        }),
      });
      setAnnotation(
        await readApiJson(response, {
          validate: isAnnotation,
          failureMessage: "Correction could not be saved.",
        }),
      );
      setStatus(
        "Review saved. Any correction overlays preserve the original model output.",
      );
    } catch (error) {
      setStatus(
        actionError(
          error,
          "Correction could not be saved. Your entries are preserved.",
          "The save response could not be verified. Your entries are preserved; reload before trying again.",
        ),
      );
      setStatusIsError(true);
    } finally {
      actionLock.current = false;
      setSaving(false);
    }
  }

  if (!annotation) {
    return (
      <section className="panel" aria-busy={!statusIsError}>
        <h2>Annotation</h2>
        <p role={statusIsError ? "alert" : "status"}>{status}</p>
      </section>
    );
  }
  const current = annotation.effective;
  return (
    <form
      className="panel annotation-form"
      onSubmit={save}
      aria-busy={saving}
    >
      <div className="section-heading">
        <div><p className="eyebrow">Reviewed overlay</p><h2>Annotation</h2></div>
        <span className="pill">{annotation.review_status}</span>
      </div>
      <label><span>Franchise / show</span><input className="field" name="franchise" defaultValue={current.franchise ?? ""} /></label>
      <label><span>Characters, comma separated</span><input className="field" name="characters" defaultValue={(current.visible_characters ?? []).join(", ")} /></label>
      <label><span>Scene description</span><textarea name="scene_description" rows={3} defaultValue={current.scene_description ?? ""} /></label>
      <div className="two-fields">
        <label><span>Composition</span><input className="field" name="composition" defaultValue={current.composition ?? ""} /></label>
        <label><span>Tone</span><input className="field" name="tone" defaultValue={current.tone ?? ""} /></label>
      </div>
      <div className="form-actions">
        <button className="button" type="submit" disabled={saving}>
          {saving ? "Saving review…" : "Save review"}
        </button>
        <small role={statusIsError ? "alert" : "status"}>{status}</small>
      </div>
    </form>
  );
}
