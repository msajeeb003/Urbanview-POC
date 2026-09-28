"use client";

/**
 * AI extraction settings (`/admin/ai`, admins; not in the mock: built from its `.card`, `.tbl`,
 * `.st`, `.abtn`, `.field` and the data screen's `.docfacts`). Everything shown is
 * `GET /v1/admin/ai`:
 *
 * - the status head: "Ready for AI extraction" or "Not ready" with the required steps left, the
 *   active key's source, "Test connection" (`POST /v1/admin/ai/check`) and the last test's facts;
 * - Readiness: the API's checklist in its order (Ready / Missing / Unknown);
 * - API key: which key is active, the key saved here (write-only: `key-form.tsx`), "Remove saved
 *   key";
 * - Model: the server's settings, read-only (changed in deploy/.env);
 * - Spend so far: per AI job type and in total;
 * - Anthropic Console links.
 *
 * While a test runs the page re-reads itself every 2 s (`AutoRefresh`); when it finishes, a toast
 * gives the outcome. No key value ever reaches this component: the API sends last-4 only.
 */
import { useEffect, useRef, useState, useTransition } from "react";

import {
  checkChip,
  checklistChip,
  checkToast,
  eur,
  latency,
  maskedKey,
  missingRequired,
  SLOW_TEST_SECONDS,
  SOURCE_LABELS,
  testInFlight,
  tokens,
  waitedSeconds,
} from "@/lib/admin/ai";
import { removeKeyAction, testConnectionAction } from "@/lib/admin/ai-actions";
import { relativeTime, utcStamp } from "@/lib/admin/format";
import type { AiStatus, AiUsageRow } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { AutoRefresh } from "../data/parts";
import { AdminCard, StatusChip } from "../parts";

import { KeyForm } from "./key-form";

const ANTHROPIC_KEYS_URL = "https://console.anthropic.com/settings/keys";
const ANTHROPIC_BILLING_URL = "https://console.anthropic.com/settings/billing";

const USAGE_LABELS: Record<AiUsageRow["type"], string> = {
  extract_document: "Document extraction",
  import_market_data: "Market-data imports",
  ai_check: "Connection tests",
  total: "All AI jobs",
};

export function AiScreen({ status }: { status: AiStatus }) {
  const showToast = useShell((s) => s.showToast);
  const [pending, start] = useTransition();
  const [removing, startRemove] = useTransition();
  const check = status.check ?? null;
  const inFlight = testInFlight(check);
  const [clock, setClock] = useState<Date | null>(null);

  // The outcome toast: the test the page last saw in flight has finished (same job, new status).
  const seen = useRef<{ id: number; inFlight: boolean } | null>(null);
  useEffect(() => {
    if (check == null) return;
    const previous = seen.current;
    if (previous && previous.id === check.job.id && previous.inFlight && !inFlight) showToast(checkToast(check));
    seen.current = { id: check.job.id, inFlight };
  }, [check, inFlight, showToast]);

  // A clock ticking while a test runs, for the "waiting N s" hint (after 30 s the worker may not
  // be listening).
  useEffect(() => {
    if (!inFlight) return;
    const timer = window.setInterval(() => setClock(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, [inFlight]);
  const waited = inFlight && clock ? waitedSeconds(check, clock) : 0;

  const missing = missingRequired(status.checklist);
  const key = status.key;
  const chip = checkChip(check);
  const now = new Date();

  const runTest = () =>
    start(async () => {
      try {
        const result = await testConnectionAction();
        showToast(result.message);
      } catch {
        showToast("The data service did not answer. Try again in a moment.");
      }
    });

  const removeKey = () => {
    if (!window.confirm("Remove the key saved in this console? Extraction jobs then need another key.")) return;
    startRemove(async () => {
      try {
        const result = await removeKeyAction();
        showToast(result.message);
      } catch {
        showToast("The data service did not answer. Try again in a moment.");
      }
    });
  };

  return (
    <>
      <AutoRefresh active={inFlight} intervalMs={2000} />

      <AdminCard
        title={status.ready ? "Ready for AI extraction" : "Not ready for AI extraction"}
        sub={
          key.configured
            ? `Key ${maskedKey(key.last4)} · ${SOURCE_LABELS[key.source]} · model ${status.model.model}`
            : "No Anthropic API key: extraction jobs fail until one is set"
        }
        action={
          <button type="button" className="abtn" disabled={pending || inFlight || !key.configured} aria-busy={pending || inFlight || undefined} onClick={runTest}>
            {inFlight ? "Testing…" : "Test connection"}
          </button>
        }
      >
        {!status.ready && missing.length > 0 && (
          <div className="admin-note aisteps">
            <b>Still needed:</b>
            <ol>
              {missing.map((item) => (
                <li key={item.key}>
                  {item.label_en}
                  <span className="fmeta"> {item.detail_en}</span>
                </li>
              ))}
            </ol>
          </div>
        )}
        <dl className="docfacts">
          <div>
            <dt>Last test</dt>
            <dd>
              <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
              {check?.detail_en && !inFlight && <span className="fmeta"> {check.detail_en}</span>}
              {inFlight && waited >= SLOW_TEST_SECONDS && (
                <span className="fmeta aiwarn"> Waiting {waited} s: is a worker listening on the extraction queue?</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Model</dt>
            <dd>{check?.model_answered ?? check?.model_requested ?? "—"}</dd>
          </div>
          <div>
            <dt>Latency</dt>
            <dd className="mono">{latency(check?.latency_ms)}</dd>
          </div>
          <div>
            <dt>Tokens · cost</dt>
            <dd className="mono">
              {check ? `${tokens(check.input_tokens + check.output_tokens)} · ${eur(check.estimated_cost_eur)}` : "—"}
            </dd>
          </div>
          <div className="wide">
            <dt>When</dt>
            <dd>
              {check?.checked_at
                ? `${utcStamp(check.checked_at)} UTC (${relativeTime(check.checked_at, now)})`
                : check
                  ? `requested ${relativeTime(check.job.requested_at, now)} by ${check.job.requested_by}`
                  : "never"}
              {check && !check.matches_current_key && !inFlight && (
                <span className="fmeta"> · with a different key ({maskedKey(check.key_last4)}): test again</span>
              )}
            </dd>
          </div>
        </dl>
      </AdminCard>

      <AdminCard title="Readiness" sub="What AI extraction needs before staff can run it from Data sources">
        <table className="tbl aichecks">
          <thead>
            <tr>
              <th>Item</th>
              <th>State</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {status.checklist.map((item) => {
              const c = checklistChip(item);
              return (
                <tr key={item.key}>
                  <td>
                    <b>{item.label_en}</b>
                    {!item.required && <span className="fmeta"> · optional</span>}
                  </td>
                  <td>
                    <StatusChip tone={c.tone}>{c.label}</StatusChip>
                  </td>
                  <td>{item.detail_en}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </AdminCard>

      <AdminCard
        title="API key"
        sub="Write-only: the key is encrypted on the server and only its last 4 characters are ever shown"
        action={
          key.console_key_stored ? (
            <button type="button" className="abtn ghost" disabled={removing} onClick={removeKey}>
              {removing ? "Removing…" : "Remove saved key"}
            </button>
          ) : undefined
        }
      >
        <dl className="docfacts">
          <div>
            <dt>Active key</dt>
            <dd>
              {key.configured ? (
                <>
                  <span className="mono">{maskedKey(key.last4)}</span>
                  <span className="fmeta"> {SOURCE_LABELS[key.source]}</span>
                </>
              ) : (
                <StatusChip tone="pend">None</StatusChip>
              )}
            </dd>
          </div>
          <div>
            <dt>Server environment</dt>
            <dd>
              {key.server_env_key ? (
                <span className="mono">{maskedKey(key.server_env_last4)}</span>
              ) : (
                <span className="fmeta">ANTHROPIC_API_KEY not set</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Saved in this console</dt>
            <dd>
              {key.console_key_stored ? (
                <>
                  <span className="mono">{maskedKey(key.console_key_last4)}</span>
                  <span className="fmeta">
                    {" "}
                    by {key.set_by ?? "—"}
                    {key.set_at ? ` on ${utcStamp(key.set_at)} UTC` : ""}
                  </span>
                </>
              ) : (
                <span className="fmeta">none</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Encryption</dt>
            <dd>{status.encryption_ready ? <StatusChip tone="ok">Ready</StatusChip> : <StatusChip tone="pend">Missing</StatusChip>}</dd>
          </div>
        </dl>
        {(key.console_note_en || status.encryption_note_en) && (
          <p className="admin-note aiwarn">{key.console_note_en ?? status.encryption_note_en}</p>
        )}
        <KeyForm disabled={!status.encryption_ready} hasSavedKey={key.console_key_stored} />
        <p className="aihint ailinks">
          <a href={status.links?.api_keys_url ?? ANTHROPIC_KEYS_URL} target="_blank" rel="noreferrer noopener">
            Anthropic Console: API keys ↗
          </a>
          <a href={status.links?.billing_url ?? ANTHROPIC_BILLING_URL} target="_blank" rel="noreferrer noopener">
            Billing &amp; credit ↗
          </a>
        </p>
      </AdminCard>

      <AdminCard title="Model" sub="Read-only: set in deploy/.env, then recreate api and worker">
        <dl className="docfacts">
          <div>
            <dt>Extraction model</dt>
            <dd className="mono">{status.model.model}</dd>
          </div>
          <div>
            <dt>Effort · thinking</dt>
            <dd>
              {status.model.effort ?? "default"} · {status.model.adaptive_thinking ? "adaptive" : "off"}
            </dd>
          </div>
          <div>
            <dt>Max output tokens</dt>
            <dd className="mono">{tokens(status.model.max_tokens)}</dd>
          </div>
          <div>
            <dt>Timeout</dt>
            <dd className="mono">{status.model.timeout_seconds} s</dd>
          </div>
          <div>
            <dt>Market model</dt>
            <dd className="mono">
              {status.model.market_model}
              {status.model.market_effort ? ` · ${status.model.market_effort}` : ""}
            </dd>
          </div>
          <div>
            <dt>API host</dt>
            <dd className="mono">
              {status.model.base_url_host}
              {!status.model.base_url_is_default && <span className="fmeta aiwarn"> not the default</span>}
            </dd>
          </div>
          <div>
            <dt>Price per M tokens</dt>
            <dd className="mono">
              in {eur(status.model.price_input_eur_per_mtok)} · out {eur(status.model.price_output_eur_per_mtok)}
            </dd>
          </div>
          <div>
            <dt>Refusal fallback</dt>
            <dd>{status.model.refusal_fallback ? "on" : "off"}</dd>
          </div>
        </dl>
      </AdminCard>

      <AdminCard
        title="Spend so far"
        sub={status.usage.since ? `AI jobs since ${utcStamp(status.usage.since)} UTC · ${status.usage.note_en}` : status.usage.note_en}
      >
        <table className="tbl">
          <thead>
            <tr>
              <th>Jobs</th>
              <th>Runs</th>
              <th>Failed</th>
              <th>Tokens in · out</th>
              <th>Estimated cost</th>
              <th>Last finished</th>
            </tr>
          </thead>
          <tbody>
            {[...status.usage.rows, status.usage.total].map((row) => (
              <tr key={row.type} className={row.type === "total" ? "aitotal" : undefined}>
                <td>
                  <b>{USAGE_LABELS[row.type]}</b>
                </td>
                <td className="mono">
                  {row.jobs} <span className="fmeta">({row.succeeded} ok)</span>
                </td>
                <td className="mono">{row.failed}</td>
                <td className="mono">
                  {tokens(row.tokens_in)} · {tokens(row.tokens_out)}
                </td>
                <td className="mono">{eur(row.estimated_cost_eur)}</td>
                <td>{row.last_finished_at ? relativeTime(row.last_finished_at, now) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {status.usage.last_extraction && (
          <p className="aihint">
            Latest extraction: job {status.usage.last_extraction.job_id} ({status.usage.last_extraction.status})
            {status.usage.last_extraction.document_id != null && (
              <>
                {" "}
                for <a href={`/admin/data/documents/${status.usage.last_extraction.document_id}`}>document {status.usage.last_extraction.document_id}</a>
              </>
            )}
            , requested {relativeTime(status.usage.last_extraction.requested_at, now)}.
          </p>
        )}
      </AdminCard>

      <p className="aihint aifoot">
        Worker: {status.worker.state}
        {status.worker.workers > 0 ? ` (${status.worker.workers})` : ""} · checked {relativeTime(status.worker.checked_at, now)} · page generated{" "}
        {utcStamp(status.generated_at)} UTC
      </p>
    </>
  );
}
