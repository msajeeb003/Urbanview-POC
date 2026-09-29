"use client";

/**
 * Uploading files from the admin console: a drop zone (drag and drop or click, several files at
 * once), the list of files with their kind / role, a progress bar each, and the result of each
 * upload. Each file goes on its own to `/api/admin/files` (the Next server streams it to
 * `POST /v1/admin/files` with the staff token) through `XMLHttpRequest`, whose upload events give
 * the progress; files upload one after another.
 *
 * A file the API already knows (same SHA-256) is not an error: it answers the existing record
 * (`created: false`) and the row says "Already uploaded", with a link to the document it is on.
 */
import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactNode } from "react";

import {
  documentHref,
  explainProblem,
  FILE_ROLES,
  formatBytes,
  guessKind,
  rolesFor,
  UPLOAD_KINDS,
  type UploadKind,
} from "@/lib/admin/data";
import type { FileRole, StoredFile, UploadResult } from "@/lib/api/types";

export type UploadStatus = "waiting" | "uploading" | "done" | "duplicate" | "error";

export interface UploadItem {
  key: string;
  file: File;
  name: string;
  size: number;
  kind: UploadKind | null;
  role: FileRole;
  status: UploadStatus;
  /** 0..1 while uploading. */
  progress: number;
  stored?: StoredFile;
  error?: string;
  /** What happened after the upload (added to the document, extraction queued …). */
  note?: string;
}

interface UploadAnswer {
  status: number;
  body: unknown;
}

/** One file to `/api/admin/files` with upload progress; network trouble answers status 0. */
export function uploadFile(file: File, kind: UploadKind, onProgress: (fraction: number) => void): Promise<UploadAnswer> {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/admin/files");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => resolve({ status: 0, body: null });
    xhr.ontimeout = () => resolve({ status: 0, body: null });
    const form = new FormData();
    form.append("kind", kind);
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

function problemOf(answer: UploadAnswer): string {
  const error = (answer.body as { error?: { code?: string; message?: string; details?: unknown } } | null)?.error;
  const details = Array.isArray(error?.details)
    ? (error!.details as { msg?: string }[]).map((d) => d.msg).filter(Boolean).join("; ")
    : "";
  if (answer.status === 422 && details) return details;
  return explainProblem({ status: answer.status, code: error?.code, message: error?.message, details: error?.details });
}

let sequence = 0;

export interface UploadOptions {
  /** Role given to new PDFs (the document detail's "Add as"). */
  defaultRole?: FileRole;
  /** The kinds this drop zone takes (a planning document: PDFs, and GIS files as drawings);
   * anything else is refused on the spot. Default: every kind, by the file's extension. */
  kinds?: readonly UploadKind[];
  /** Called after each successful upload; its answer becomes the row's note. */
  onUploaded?: (item: UploadItem) => Promise<string | void> | string | void;
}

export function useUploads(options: UploadOptions = {}) {
  const queue = useRef<UploadItem[]>([]);
  const running = useRef(false);
  const latest = useRef(options);
  useEffect(() => {
    latest.current = options;
  });
  const [items, setItems] = useState<UploadItem[]>([]);

  const publish = useCallback(() => setItems(queue.current.map((i) => ({ ...i }))), []);
  const patch = useCallback(
    (key: string, change: Partial<UploadItem>) => {
      queue.current = queue.current.map((i) => (i.key === key ? { ...i, ...change } : i));
      publish();
    },
    [publish],
  );

  const add = useCallback((files: FileList | File[], role?: FileRole): UploadItem[] => {
    const { kinds, defaultRole = "text" } = latest.current;
    const added = Array.from(files).map((file): UploadItem => {
      const guessed = guessKind(file.name);
      const kind = guessed && (!kinds || kinds.includes(guessed)) ? guessed : null;
      const refused = !kind
        ? kinds
          ? kinds.includes("gis")
            ? "A drawing is a PDF or a GIS file (.gpkg, .geojson, zipped Shapefile)."
            : "Only PDF files belong here; add GIS files as drawings."
          : "This file type is not accepted (PDF, GIS files or cadastral extracts)."
        : file.size === 0
          ? "The file is empty."
          : undefined;
      return {
        key: `u${++sequence}`,
        file,
        name: file.name,
        size: file.size,
        kind,
        role: kind === "gis" ? "drawing" : (role ?? defaultRole),
        status: refused ? "error" : "waiting",
        progress: 0,
        error: refused,
      };
    });
    queue.current = [...queue.current, ...added];
    publish();
    return added;
  }, [publish]);

  const start = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    try {
      for (;;) {
        const next = queue.current.find((i) => i.status === "waiting" && i.kind);
        if (!next || !next.kind) break;
        patch(next.key, { status: "uploading", progress: 0 });
        const answer = await uploadFile(next.file, next.kind, (fraction) => patch(next.key, { progress: fraction }));
        if (answer.status === 200 || answer.status === 201) {
          const stored = (answer.body as UploadResult).file;
          const status: UploadStatus = answer.status === 201 ? "done" : "duplicate";
          patch(next.key, { status, progress: 1, stored });
          const current = queue.current.find((i) => i.key === next.key)!;
          try {
            const note = await latest.current.onUploaded?.(current);
            if (note) patch(next.key, { note });
          } catch {
            patch(next.key, { note: "Uploaded, but the next step failed; try again from the table." });
          }
        } else {
          patch(next.key, { status: "error", progress: 0, error: problemOf(answer) });
        }
      }
    } finally {
      running.current = false;
    }
  }, [patch]);

  const setKind = useCallback((key: string, kind: UploadKind) => {
    const item = queue.current.find((i) => i.key === key);
    if (!item || item.status !== "waiting") return;
    patch(key, { kind, role: kind === "gis" ? "drawing" : item.role });
  }, [patch]);

  const setRole = useCallback((key: string, role: FileRole) => patch(key, { role }), [patch]);

  const remove = useCallback((key: string) => {
    queue.current = queue.current.filter((i) => i.key !== key || i.status === "uploading");
    publish();
  }, [publish]);

  const clearFinished = useCallback(() => {
    queue.current = queue.current.filter((i) => i.status === "waiting" || i.status === "uploading");
    publish();
  }, [publish]);

  const reset = useCallback(() => {
    if (running.current) return;
    queue.current = [];
    publish();
  }, [publish]);

  return { items, add, start, setKind, setRole, remove, clearFinished, reset, busy: items.some((i) => i.status === "uploading") };
}

export function DropZone({
  accept,
  onFiles,
  title = "Drop files here or click to choose",
  hint,
  disabled,
  children,
}: {
  accept: string;
  onFiles: (files: File[]) => void;
  title?: string;
  hint?: ReactNode;
  disabled?: boolean;
  children?: ReactNode;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const take = (list: FileList | null) => {
    if (!list || !list.length || disabled) return;
    onFiles(Array.from(list));
  };
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setOver(false);
    take(event.dataTransfer.files);
  };
  return (
    <div
      className={["dropzone", over ? "over" : "", disabled ? "disabled" : ""].filter(Boolean).join(" ")}
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled || undefined}
      onClick={() => !disabled && input.current?.click()}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && !disabled) {
          e.preventDefault();
          input.current?.click();
        }
      }}
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
    >
      <input
        ref={input}
        type="file"
        multiple
        accept={accept}
        hidden
        onChange={(e) => {
          take(e.target.files);
          e.target.value = "";
        }}
      />
      <div className="dz-title">{title}</div>
      {hint && <div className="dz-hint">{hint}</div>}
      {children}
    </div>
  );
}

function statusText(item: UploadItem): ReactNode {
  switch (item.status) {
    case "waiting":
      return "Ready to upload";
    case "uploading":
      return `Uploading ${Math.round(item.progress * 100)}%`;
    case "done":
      return item.note ?? "Uploaded";
    case "duplicate": {
      const documents = item.stored?.document_ids ?? [];
      return (
        <>
          Already uploaded{item.stored ? ` · file #${item.stored.id}` : ""}
          {item.note ? ` · ${item.note}` : documents.length ? (
            <>
              {" · "}
              <Link href={documentHref(documents[0])}>open its document →</Link>
            </>
          ) : (
            " · not on a document yet"
          )}
        </>
      );
    }
    default:
      return item.error ?? "Not uploaded";
  }
}

export function UploadList({
  items,
  showKind,
  showRole,
  onKind,
  onRole,
  onRemove,
}: {
  items: UploadItem[];
  showKind?: boolean;
  showRole?: boolean;
  onKind?: (key: string, kind: UploadKind) => void;
  onRole?: (key: string, role: FileRole) => void;
  onRemove?: (key: string) => void;
}) {
  if (!items.length) return null;
  return (
    <ul className="uplist" aria-live="polite">
      {items.map((item) => (
        <li key={item.key} className={`uprow ${item.status}`}>
          <div className="up-main">
            <span className="up-name" title={item.name}>
              {item.name}
            </span>
            <span className="up-size mono">{formatBytes(item.size)}</span>
            {showKind && (
              <select
                aria-label={`Kind of ${item.name}`}
                value={item.kind ?? ""}
                disabled={item.status !== "waiting"}
                onChange={(e) => onKind?.(item.key, e.target.value as UploadKind)}
              >
                {!item.kind && <option value="">Not accepted</option>}
                {UPLOAD_KINDS.map((k) => (
                  <option key={k.value} value={k.value}>
                    {k.label}
                  </option>
                ))}
              </select>
            )}
            {showRole && item.kind && (
              <select
                aria-label={`Role of ${item.name}`}
                value={item.role}
                disabled={item.status === "uploading"}
                onChange={(e) => onRole?.(item.key, e.target.value as FileRole)}
              >
                {FILE_ROLES.filter((r) => rolesFor(item.kind ?? "").includes(r.value)).map((r) => (
                  <option key={r.value} value={r.value} title={r.hint}>
                    {r.label}
                  </option>
                ))}
              </select>
            )}
            {onRemove && item.status !== "uploading" && (
              <button type="button" className="up-x" aria-label={`Remove ${item.name}`} onClick={() => onRemove(item.key)}>
                ✕
              </button>
            )}
          </div>
          <div className="up-bar" aria-hidden="true">
            <i style={{ width: `${Math.round((item.status === "waiting" ? 0 : item.progress) * 100)}%` }} />
          </div>
          <div className="up-status">{statusText(item)}</div>
        </li>
      ))}
    </ul>
  );
}
