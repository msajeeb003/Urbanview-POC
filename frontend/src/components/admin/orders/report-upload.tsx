"use client";

/**
 * "Upload report": drop the finished PDF (or click to choose), optionally with a note, and send it
 * with a progress bar to `/api/admin/orders/[id]/report` (streamed to the API with the staff
 * token). The API stores it, delivers the order and e-mails the customer the download link.
 * Replacing a delivered report needs a note (it is versioned and the new link is e-mailed again).
 */
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useShell } from "@/lib/store";

import { DropZone } from "../data/uploads";

function send(url: string, form: FormData, onProgress: (fraction: number) => void): Promise<{ status: number; body: unknown }> {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) onProgress(e.loaded / e.total);
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
    xhr.send(form);
  });
}

function refusal(answer: { status: number; body: unknown }): string {
  const error = (answer.body as { error?: { message?: string; details?: unknown } } | null)?.error;
  if (answer.status === 0) return "The upload did not reach the server. Try again.";
  if (answer.status === 403) return "This order is not assigned to you.";
  if (answer.status === 413) return "The PDF is larger than the upload limit.";
  if (answer.status === 422 && Array.isArray(error?.details)) {
    return (error!.details as { msg?: string }[]).map((d) => d.msg).filter(Boolean).join("; ") || "Not accepted.";
  }
  return error?.message ?? "The report was not accepted.";
}

export function ReportUpload({ orderId, replacing, disabled, reason }: { orderId: number; replacing: boolean; disabled: boolean; reason?: string }) {
  const router = useRouter();
  const showToast = useShell((s) => s.showToast);
  const [file, setFile] = useState<File | null>(null);
  const [note, setNote] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const upload = async () => {
    if (!file) return;
    if (replacing && !note.trim()) {
      setMessage("Say why the delivered report is replaced: the note goes to the audit log.");
      return;
    }
    const form = new FormData();
    form.append("file", file, file.name);
    if (note.trim()) form.append("note", note.trim());
    setMessage(null);
    setProgress(0);
    const answer = await send(`/api/admin/orders/${orderId}/report`, form, setProgress);
    setProgress(null);
    if (answer.status === 200) {
      showToast(replacing ? "Report replaced — the new link was e-mailed" : "Report delivered — the customer was e-mailed");
      setFile(null);
      setNote("");
      router.refresh();
    } else {
      setMessage(refusal(answer));
    }
  };

  if (disabled) return <div className="ohint" title={reason}>{reason ?? "The report cannot be uploaded now."}</div>;
  return (
    <div className="oupload">
      {file ? (
        <div className="opicked">
          <span className="up-name" title={file.name}>
            {file.name}
          </span>
          <span className="mono up-size">{(file.size / (1024 * 1024)).toFixed(1)} MB</span>
          <button type="button" className="up-x" aria-label="Choose another file" onClick={() => setFile(null)} disabled={progress != null}>
            ✕
          </button>
        </div>
      ) : (
        <DropZone
          accept=".pdf,application/pdf"
          title={replacing ? "Drop the corrected report (PDF) or click to choose" : "Drop the finished report (PDF) or click to choose"}
          hint="The customer receives a download link by e-mail as soon as it is uploaded."
          onFiles={(files) => {
            const pdf = files.find((f) => f.name.toLowerCase().endsWith(".pdf"));
            setFile(pdf ?? null);
            setMessage(pdf ? null : "Only a PDF report is accepted.");
          }}
        />
      )}
      <textarea
        className="rinput rnote"
        rows={2}
        maxLength={2000}
        placeholder={replacing ? "Note (required): why the report is replaced" : "Note for the audit log (optional)"}
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      {progress != null && (
        <div className="up-bar" aria-label="Upload progress">
          <i style={{ width: `${Math.round(progress * 100)}%` }} />
        </div>
      )}
      {message && <div className="rmsg">{message}</div>}
      <div className="rbtns">
        <button type="button" className="abtn sm" disabled={!file || progress != null} onClick={() => void upload()}>
          {progress != null ? `Uploading ${Math.round(progress * 100)}%` : replacing ? "Replace report" : "Upload and deliver"}
        </button>
      </div>
    </div>
  );
}
