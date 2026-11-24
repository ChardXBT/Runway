"use client";

/** Resumable chunked media upload against `POST /api/v1/media`.
 *
 * Flow: sha256 → create (returns an upload plan) → send only missing parts
 * (`api` mode puts bytes on `/parts/{n}`; `direct` mode puts them on the
 * signed part URLs and completes with the returned etags) → complete with the
 * same sha256. A failed part re-queries `/upload` and only missing parts are
 * resent, so a retry never restarts a large upload.
 */

import { ApiError } from "@/lib/client-api";
import { V1 } from "@/lib/v1-client";
import type { MediaOut, PartIn, UploadPlanOut } from "@/lib/v1-guards";

export interface UploadProgress {
  bytesSent: number;
  bytesTotal: number;
  partsDone: number;
  partsTotal: number;
  phase: "hashing" | "registering" | "uploading" | "completing";
}

export interface UploadableFile {
  name: string;
  type: string;
  size: number;
  slice(start: number, end: number): Blob;
  arrayBuffer(): Promise<ArrayBuffer>;
}

export interface UploadOptions {
  onProgress?: (progress: UploadProgress) => void;
  signal?: AbortSignal;
  /** Injectable for tests; defaults to WebCrypto SHA-256. */
  digest?: (data: ArrayBuffer) => Promise<ArrayBuffer>;
  /** Injectable direct-mode PUT (signed URLs are not Runway routes). */
  directPut?: (url: string, body: Blob, signal?: AbortSignal) => Promise<string | null>;
}

export async function sha256Hex(
  file: UploadableFile,
  digest?: (data: ArrayBuffer) => Promise<ArrayBuffer>,
): Promise<string> {
  const buffer = await file.arrayBuffer();
  const digestFn =
    digest ??
    ((data: ArrayBuffer) => {
      if (typeof crypto === "undefined" || !crypto.subtle) {
        throw new ApiError(
          "This browser cannot hash files; use a current browser to upload.",
        );
      }
      return crypto.subtle.digest("SHA-256", data);
    });
  const hash = await digestFn(buffer);
  return [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function defaultDirectPut(
  url: string,
  body: Blob,
  signal?: AbortSignal,
): Promise<string | null> {
  const response = await fetch(url, { method: "PUT", body, signal });
  if (!response.ok) {
    throw new ApiError(`The media host refused a part (${response.status}); retry the upload.`, {
      uncertainOutcome: response.status >= 500,
    });
  }
  return response.headers.get("ETag");
}

function throwIfAborted(signal?: AbortSignal) {
  if (signal?.aborted) {
    throw new ApiError("Upload cancelled.");
  }
}

/** Upload one file end to end; resolves to the asset row once complete. */
export async function uploadMediaAsset(
  file: UploadableFile,
  options: UploadOptions = {},
): Promise<MediaOut> {
  const { onProgress, signal, digest, directPut = defaultDirectPut } = options;
  const report = (progress: UploadProgress) => onProgress?.(progress);

  report({ bytesSent: 0, bytesTotal: file.size, partsDone: 0, partsTotal: 0, phase: "hashing" });
  const sha256 = await sha256Hex(file, digest);
  throwIfAborted(signal);

  report({ bytesSent: 0, bytesTotal: file.size, partsDone: 0, partsTotal: 0, phase: "registering" });
  const asset = await V1.media.create({
    filename: file.name,
    content_type: file.type || "application/octet-stream",
    size_bytes: file.size,
    sha256,
  });
  const plan = asset.upload;
  if (!plan) {
    // The asset already had its bytes (same sha256) — nothing to send.
    report({
      bytesSent: file.size,
      bytesTotal: file.size,
      partsDone: 0,
      partsTotal: 0,
      phase: "completing",
    });
    return asset;
  }
  throwIfAborted(signal);

  let completed: MediaOut;
  report({ ...progressFor(file.size, 0, plan), phase: "uploading" });
  if (plan.mode === "api") {
    await uploadApiParts(asset.id, file, plan, report, signal);
    report({ ...progressFor(file.size, plan.part_count, plan), phase: "completing" });
    throwIfAborted(signal);
    completed = await V1.media.complete(asset.id, { sha256 });
  } else {
    completed = await uploadDirectParts(asset.id, file, plan, sha256, directPut, report, signal);
  }
  if (completed.status === "rejected" && completed.rejected_reason) {
    throw new ApiError(`The media was rejected: ${completed.rejected_reason}`);
  }
  return completed;
}

function progressFor(
  bytesTotal: number,
  partsDone: number,
  plan: UploadPlanOut,
): Omit<UploadProgress, "phase"> {
  return {
    bytesSent: Math.min(partsDone * plan.part_size, bytesTotal),
    bytesTotal,
    partsDone,
    partsTotal: plan.part_count,
  };
}

async function uploadApiParts(
  assetId: number,
  file: UploadableFile,
  plan: UploadPlanOut,
  report: (progress: UploadProgress) => void,
  signal?: AbortSignal,
) {
  let status = await V1.media.uploadStatus(assetId);
  for (const part of plan.part_count ? missingParts(plan, status.missing_parts) : []) {
    throwIfAborted(signal);
    const start = (part - 1) * plan.part_size;
    const bytes = file.slice(start, Math.min(start + plan.part_size, file.size));
    await V1.media.putPart(assetId, part, bytes);
    status = {
      ...status,
      received_parts: [...status.received_parts, part],
      missing_parts: status.missing_parts.filter((missing) => missing !== part),
    };
    const partsDone = status.received_parts.length;
    report({
      bytesSent: Math.min(partsDone * plan.part_size, file.size),
      bytesTotal: file.size,
      partsDone,
      partsTotal: plan.part_count,
      phase: "uploading",
    });
  }
}

function missingParts(plan: UploadPlanOut, missing: number[]): number[] {
  const set = new Set(missing.length ? missing : Array.from({ length: plan.part_count }, (_, i) => i + 1));
  return [...set].sort((a, b) => a - b);
}

async function uploadDirectParts(
  assetId: number,
  file: UploadableFile,
  plan: UploadPlanOut,
  sha256: string,
  directPut: (url: string, body: Blob, signal?: AbortSignal) => Promise<string | null>,
  report: (progress: UploadProgress) => void,
  signal?: AbortSignal,
) {
  const urls = plan.part_urls ?? {};
  const parts: PartIn[] = [];
  for (let part = 1; part <= plan.part_count; part += 1) {
    throwIfAborted(signal);
    const url = urls[String(part)];
    if (!url) {
      throw new ApiError("The upload plan did not include every part URL; retry the upload.");
    }
    const start = (part - 1) * plan.part_size;
    const bytes = file.slice(start, Math.min(start + plan.part_size, file.size));
    const etag = await directPut(url, bytes, signal);
    parts.push({ part_number: part, etag: etag ?? "", size: bytes.size });
    report({ ...progressFor(file.size, part, plan), phase: "uploading" });
  }
  report({ ...progressFor(file.size, plan.part_count, plan), phase: "completing" });
  return V1.media.complete(assetId, { sha256, parts });
}
