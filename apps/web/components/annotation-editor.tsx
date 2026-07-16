"use client";

import { FormEvent, useEffect, useState } from "react";

import { API_URL } from "@/lib/api";

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

export function AnnotationEditor({ postId }: { postId: number }) {
  const [annotation, setAnnotation] = useState<Annotation | null>(null);
  const [status, setStatus] = useState("Loading annotation…");

  useEffect(() => {
    fetch(`${API_URL}/api/annotations/${postId}`)
      .then((response) => (response.ok ? response.json() : Promise.reject()))
      .then((value: Annotation) => {
        setAnnotation(value);
        setStatus("");
      })
      .catch(() => setStatus("Run historical analysis to create this annotation."));
  }, [postId]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
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
    setStatus("Saving…");
    const response = await fetch(`${API_URL}/api/annotations/${postId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields }),
    });
    if (!response.ok) {
      setStatus("Correction could not be saved.");
      return;
    }
    setAnnotation((await response.json()) as Annotation);
    setStatus("Correction saved. The original model output remains in history.");
  }

  if (!annotation) return <section className="panel"><h2>Annotation</h2><p>{status}</p></section>;
  const current = annotation.effective;
  return (
    <form className="panel annotation-form" onSubmit={save}>
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
      <div className="form-actions"><button className="button" type="submit">Save correction</button><small role="status">{status}</small></div>
    </form>
  );
}
