"use client";
/* eslint-disable @next/next/no-img-element */

/** Media picker: chunked resumable uploads with progress, plus the post's
 * existing media list. Files hash locally, upload through `/api/v1/media`
 * parts, then complete — a finished asset attaches to the post by id.
 */

import { useRef, useState } from "react";

import { actionError } from "@/lib/client-api";
import { uploadMediaAsset, type UploadProgress } from "@/lib/media-upload";
import { V1 } from "@/lib/v1-client";
import type { MediaRefOut } from "@/lib/v1-guards";

export interface PickedMedia {
  asset_id: number;
  kind: string | null;
  status: string;
  label: string;
}

export function mediaLabel(media: MediaRefOut | PickedMedia): string {
  const kind = media.kind ?? "file";
  return `${kind} · asset ${media.asset_id}`;
}

interface PendingUpload {
  name: string;
  progress: UploadProgress;
  error: string | null;
}

export function MediaPicker({
  media,
  onAdded,
}: {
  media: PickedMedia[];
  onAdded: (item: PickedMedia) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploads, setUploads] = useState<PendingUpload[]>([]);

  const upload = async (file: File) => {
    const entry: PendingUpload = {
      name: file.name,
      progress: {
        bytesSent: 0,
        bytesTotal: file.size,
        partsDone: 0,
        partsTotal: 0,
        phase: "hashing",
      },
      error: null,
    };
    setUploads((current) => [...current, entry]);
    const patch = (next: Partial<PendingUpload>) =>
      setUploads((current) =>
        current.map((item) => (item === entry ? { ...item, ...next } : item)),
      );
    try {
      const asset = await uploadMediaAsset(file, {
        onProgress: (progress) => patch({ progress }),
      });
      setUploads((current) => current.filter((item) => item !== entry));
      onAdded({
        asset_id: asset.id,
        kind: asset.kind,
        status: asset.status,
        label: file.name,
      });
    } catch (err) {
      patch({
        error: actionError(
          err,
          "The upload failed; try again — finished parts are kept.",
        ),
      });
    }
  };

  return (
    <div className="media-picker">
      <ul className="media-list clean-list">
        {media.map((item) => (
          <li key={item.asset_id} className="media-row">
            <img
              src={V1.media.posterUrl(item.asset_id)}
              alt=""
              className="media-thumb"
              loading="lazy"
              onError={(event) => {
                event.currentTarget.style.visibility = "hidden";
              }}
            />
            <span>
              {item.label}
              <small>
                {item.kind ?? "media"} · {item.status}
              </small>
            </span>
          </li>
        ))}
      </ul>
      {uploads.map((upload) => (
        <div key={upload.name} className="media-upload-row">
          <span>
            {upload.name}
            <small>
              {upload.progress.phase === "uploading"
                ? `${upload.progress.partsDone}/${upload.progress.partsTotal} parts`
                : upload.progress.phase}
            </small>
          </span>
          <progress
            value={upload.progress.bytesSent}
            max={upload.progress.bytesTotal}
            aria-label={`Upload progress for ${upload.name}`}
          />
          {upload.error ? (
            <small className="text-danger" role="alert">
              {upload.error}
            </small>
          ) : null}
        </div>
      ))}
      <input
        ref={inputRef}
        type="file"
        multiple
        className="visually-hidden"
        onChange={(event) => {
          const files = [...(event.target.files ?? [])];
          event.target.value = "";
          files.forEach((file) => void upload(file));
        }}
      />
      <button
        type="button"
        className="button secondary"
        onClick={() => inputRef.current?.click()}
      >
        Add media
      </button>
    </div>
  );
}
