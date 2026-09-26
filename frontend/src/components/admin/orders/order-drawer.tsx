"use client";

/**
 * One order (the Orders tab's drawer, opened by `?order=<id>`): who ordered, the parcel (with a
 * link that opens the map there), price and turnaround, payment, fulfilment, the e-mails sent, the
 * snapshot the customer saw (read-only: the expert works from what was shown) and the timeline
 * from the audit log.
 *
 * Payment (admins and reviewers): "Mark payment received" (amount, date, bank reference — all
 * required), "Payment not received" (a note: the check is recorded, the order stays pending) and
 * "Refund" (amount, date, reference); each asks for confirmation first. Fulfilment: assign an
 * expert (a paid order starts), "Start", and the report upload (also the assigned expert's only
 * action). Every action follows the API's status flow; a disabled one says why.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, useTransition, type ReactNode } from "react";

import { relativeTime, utcStamp } from "@/lib/admin/format";
import { assignAction, paymentAction, startAction, type PaymentInput } from "@/lib/admin/order-actions";
import {
  allowed,
  customerLine,
  daysSince,
  emailChip,
  emailName,
  eventLine,
  mapHref,
  parcelLine,
  snapshotAssumptions,
  snapshotFeasibility,
  snapshotPlanning,
  statusChip,
  type SnapshotRow,
} from "@/lib/admin/orders";
import type { StaffRole } from "@/lib/admin/sections";
import type { OrderDetail, OrderExpert } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { StatusChip } from "../parts";

import { ReportUpload } from "./report-upload";

function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="osect">
      <div className="osect-h">
        <h4>{title}</h4>
        {aside}
      </div>
      {children}
    </section>
  );
}

function Rows({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="orows">
      {rows
        .filter(([, v]) => v != null && v !== "")
        .map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
    </dl>
  );
}

function SnapshotTable({ rows, empty }: { rows: SnapshotRow[]; empty: string }) {
  if (!rows.length) return <div className="ohint">{empty}</div>;
  return (
    <table className="tbl otbl">
      <tbody>
        {rows.map((r, i) => (
          <tr key={`${r.label}-${i}`}>
            <td>{r.label}</td>
            <td className="mono">{r.value}</td>
            <td className="osub">{r.note ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const TODAY = () => new Date().toISOString().slice(0, 10);

function Payment({ order }: { order: OrderDetail }) {
  const showToast = useShell((s) => s.showToast);
  const [kind, setKind] = useState<PaymentInput["kind"] | null>(null);
  const [amount, setAmount] = useState(String(order.payment_amount_eur ?? order.price_eur));
  const [date, setDate] = useState(TODAY());
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();
  const receive = allowed(order, "receive", "admin");
  const refund = allowed(order, "refund", "admin");

  const choose = (next: PaymentInput["kind"]) => {
    setKind(next);
    setConfirming(false);
    setMessage(null);
    setAmount(String(next === "refunded" ? (order.payment_amount_eur ?? order.price_eur) : order.price_eur));
  };
  const input: PaymentInput | null = kind
    ? { kind, amount: amount ? Number(amount.replace(",", ".")) : null, date, reference, note }
    : null;
  const check = (): string | null => {
    if (!input) return null;
    if (input.kind === "not_received") return input.note.trim() ? null : "Say what was checked.";
    if (!(input.amount && input.amount > 0)) return "Enter the amount.";
    if (!input.date) return "Enter the date.";
    if (!input.reference.trim()) return "Enter the bank reference.";
    return null;
  };
  const summary =
    input?.kind === "received"
      ? `Mark ${order.reference} as paid: €${input.amount} received on ${input.date}, ref ${input.reference}?`
      : input?.kind === "refunded"
        ? `Refund ${order.reference}: €${input.amount} on ${input.date}, ref ${input.reference}? The order closes as refunded.`
        : `Record that the payment for ${order.reference} has not arrived? The order stays pending.`;

  const submit = () =>
    start(async () => {
      if (!input) return;
      const result = await paymentAction(order.id, input);
      showToast(result.message);
      if (result.ok) {
        setKind(null);
        setConfirming(false);
        setReference("");
        setNote("");
      } else {
        setConfirming(false);
        setMessage(result.message);
      }
    });

  return (
    <Section title="Payment" aside={order.paid_at ? <span className="osub">paid {utcStamp(order.paid_at)}</span> : undefined}>
      <Rows
        rows={[
          ["Received", order.payment_amount_eur != null ? `€${order.payment_amount_eur} on ${order.payment_received_on ?? "—"}` : ""],
          ["Bank reference", order.payment_reference ?? ""],
          ["Refunded", order.refunded_at ? utcStamp(order.refunded_at) : ""],
          ["Notes", order.notes ? <span className="opre">{order.notes}</span> : ""],
        ]}
      />
      <div className="oacts" role="group" aria-label="Record a payment">
        <button type="button" className={kind === "received" ? "abtn sm" : "abtn sm ghost"} disabled={!receive.enabled} title={receive.reason} onClick={() => choose("received")}>
          Mark payment received
        </button>
        <button type="button" className={kind === "not_received" ? "abtn sm" : "abtn sm ghost"} disabled={!receive.enabled} title={receive.reason} onClick={() => choose("not_received")}>
          Payment not received
        </button>
        <button type="button" className={kind === "refunded" ? "abtn sm" : "abtn sm ghost"} disabled={!refund.enabled} title={refund.reason} onClick={() => choose("refunded")}>
          Refund
        </button>
      </div>
      {kind && (
        <div className="reditor">
          {kind !== "not_received" && (
            <div className="ofields">
              <label>
                <span className="fieldlab">Amount (€)</span>
                <input className="rinput" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
              </label>
              <label>
                <span className="fieldlab">{kind === "refunded" ? "Refunded on" : "Received on"}</span>
                <input className="rinput" type="date" value={date} max={TODAY()} onChange={(e) => setDate(e.target.value)} />
              </label>
              <label>
                <span className="fieldlab">Bank reference</span>
                <input className="rinput" value={reference} maxLength={100} placeholder="e.g. 2609-4471" onChange={(e) => setReference(e.target.value)} />
              </label>
            </div>
          )}
          <textarea
            className="rinput rnote"
            rows={2}
            maxLength={2000}
            placeholder={kind === "not_received" ? "What was checked (required), e.g. “nothing on the statement of 26 Sep”" : "Note for the audit log (optional)"}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          {message && <div className="rmsg">{message}</div>}
          {confirming ? (
            <div className="oconfirm">
              <span>{summary}</span>
              <div className="rbtns">
                <button type="button" className="abtn sm" disabled={pending} onClick={submit}>
                  {pending ? "Recording…" : "Confirm"}
                </button>
                <button type="button" className="abtn sm ghost" onClick={() => setConfirming(false)}>
                  Back
                </button>
              </div>
            </div>
          ) : (
            <div className="rbtns">
              <button
                type="button"
                className="abtn sm"
                onClick={() => {
                  const problem = check();
                  setMessage(problem);
                  if (!problem) setConfirming(true);
                }}
              >
                Continue
              </button>
              <button type="button" className="abtn sm ghost" onClick={() => setKind(null)}>
                Cancel
              </button>
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

function Fulfilment({ order, experts, role }: { order: OrderDetail; experts: OrderExpert[]; role: StaffRole }) {
  const router = useRouter();
  const showToast = useShell((s) => s.showToast);
  const [expert, setExpert] = useState<string>(order.assignee ? String(order.assignee.user_id) : "");
  const [pending, start] = useTransition();
  const assign = allowed(order, "assign", role);
  const begin = allowed(order, "start", role);
  const upload = allowed(order, "upload", role);
  const run = (action: () => Promise<{ ok: boolean; message: string }>) =>
    start(async () => {
      const result = await action();
      showToast(result.message);
      if (result.ok) router.refresh();
    });

  return (
    <Section title="Fulfilment" aside={order.assignee ? <span className="osub">expert: {order.assignee.display_name ?? order.assignee.email}</span> : undefined}>
      {assign.visible && (
        <div className="oacts">
          <select className="rinput oselect" aria-label="Expert" value={expert} disabled={!assign.enabled || pending} title={assign.reason} onChange={(e) => setExpert(e.target.value)}>
            <option value="">{experts.length ? "Choose an expert" : "No active experts"}</option>
            {experts.map((x) => (
              <option key={x.user_id} value={x.user_id}>
                {x.display_name ?? x.email} · {x.open_orders} in progress
              </option>
            ))}
          </select>
          <button
            type="button"
            className="abtn sm"
            disabled={!assign.enabled || pending || !expert || Number(expert) === order.assignee?.user_id}
            title={assign.reason}
            onClick={() => run(() => assignAction(order.id, Number(expert), ""))}
          >
            {order.assignee ? "Reassign" : "Assign expert"}
          </button>
          {begin.visible && (
            <button type="button" className="abtn sm ghost" disabled={!begin.enabled || pending} title={begin.reason} onClick={() => run(() => startAction(order.id))}>
              Start
            </button>
          )}
        </div>
      )}
      <div className="oreport">
        {order.report ? (
          <div className="ohave">
            <span className="mono">
              {order.report.original_filename}
              {order.report_versions && order.report_versions > 1 ? ` · v${order.report_versions}` : ""}
            </span>
            <span className="osub"> uploaded {relativeTime(order.report.uploaded_at)}</span>
            {order.report.download_url && (
              <a className="abtn sm ghost" href={order.report.download_url} target="_blank" rel="noreferrer">
                Download ↗
              </a>
            )}
          </div>
        ) : null}
        <ReportUpload orderId={order.id} replacing={order.status === "delivered"} disabled={!upload.enabled} reason={upload.reason} />
      </div>
    </Section>
  );
}

export function OrderDrawer({
  order,
  experts,
  role,
  closeHref,
}: {
  order: OrderDetail;
  experts: OrderExpert[];
  role: StaffRole;
  closeHref: string;
}) {
  const chip = statusChip(order.status);
  const legal = order.purchaser_type === "legal_entity";
  const map = mapHref(order.location);
  const manager = role === "admin" || role === "reviewer";
  const snapshot = (order.snapshot ?? {}) as Record<string, unknown>;
  const planning = snapshotPlanning(snapshot);
  const figures = snapshotFeasibility(snapshot);
  const assumptions = snapshotAssumptions(snapshot, (order.assumption_edits ?? {}) as Record<string, unknown>);

  return (
    <>
      <Link className="oscrim" href={closeHref} aria-label="Close the order" scroll={false} />
      <aside className="odrawer" role="dialog" aria-label={`Order ${order.reference}`}>
        <div className="ohead">
          <div>
            <div className="reyebrow mono">Expert analysis order</div>
            <h2 className="rtitle mono">{order.reference}</h2>
            <div className="osub">
              placed {utcStamp(order.placed_at)} · {daysSince(order.placed_at)} days ago · expected by {order.expected_by}
            </div>
          </div>
          <div className="ohead-r">
            <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
            <Link className="up-x" href={closeHref} aria-label="Close" scroll={false}>
              ✕
            </Link>
          </div>
        </div>
        <div className="obody">
          <Section title="Customer">
            <Rows
              rows={
                legal
                  ? [
                      ["Company", order.company_name ?? ""],
                      ["PIB / VAT", <span className="mono" key="t">{order.tax_number}</span>],
                      ["Contact person", order.contact_person ?? ""],
                      ["Registered address", order.registered_address ?? ""],
                      ["E-mail", order.email],
                      ["Telephone", <span className="mono" key="p">{order.telephone}</span>],
                    ]
                  : [
                      ["Name", customerLine(order)],
                      ["E-mail", order.email],
                      ["Telephone", <span className="mono" key="p">{order.telephone}</span>],
                    ]
              }
            />
            {order.message && <p className="omsg">“{order.message}”</p>}
          </Section>

          <Section
            title="Location ordered"
            aside={
              map ? (
                <a className="abtn sm ghost" href={map} target="_blank" rel="noreferrer" title="Opens the public map at this parcel (today's published data)">
                  Open on the map ↗
                </a>
              ) : undefined
            }
          >
            <Rows
              rows={[
                ["Parcel", <span className="mono" key="p">{parcelLine(order.location)}</span>],
                ["Type", order.location.parcel_type === "urban" ? "Urban (planned) parcel" : "Cadastral parcel"],
                ["Planning document", order.location.document_name ?? ""],
                ["Zone", order.location.zone_name ?? ""],
              ]}
            />
          </Section>

          <Section title="Price and turnaround">
            <Rows
              rows={[
                ["Price", <span className="mono" key="p">€{order.price_eur} {order.currency !== "EUR" ? order.currency : ""}</span>],
                ["Turnaround", `${order.turnaround.business_days} business days · expected by ${order.expected_by}`],
              ]}
            />
          </Section>

          {manager && <Payment order={order} />}
          <Fulfilment order={order} experts={experts} role={role} />

          <Section title="E-mails">
            {order.emails && order.emails.length ? (
              <ul className="olist">
                {order.emails.map((e) => {
                  const c = emailChip(e);
                  return (
                    <li key={e.id}>
                      <StatusChip tone={c.tone}>{c.label}</StatusChip> <b>{emailName(e.template)}</b>
                      <span className="osub">
                        {" "}
                        to {e.to_email} · {utcStamp(e.sent_at ?? e.created_at)}
                        {e.bounce_reason ? ` · bounced: ${e.bounce_reason}` : e.error ? ` · ${e.error.slice(0, 120)}` : e.suppressed_reason ? ` · ${e.suppressed_reason.replace(/_/g, " ")}` : ""}
                      </span>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <div className="ohint">No e-mail recorded for this order.</div>
            )}
          </Section>

          <Section
            title="What the customer saw"
            aside={
              <span className="osub mono">
                data {order.data_version ?? "—"} · market v{order.market_version ?? "—"} · formula {order.formula_version ?? "—"}
              </span>
            }
          >
            <p className="ohint">The panel as it was when the order was placed, with the customer&apos;s own assumptions. Read-only.</p>
            <details className="odetails" open>
              <summary>Planning parameters (Group 1)</summary>
              <SnapshotTable rows={planning} empty="No planning values were shown." />
            </details>
            <details className="odetails">
              <summary>Assumptions</summary>
              <SnapshotTable rows={assumptions} empty="No market assumptions were shown." />
            </details>
            <details className="odetails">
              <summary>Market figures (Group 2, low – expected – high)</summary>
              <SnapshotTable rows={figures} empty="No figures were calculated for this parcel." />
            </details>
          </Section>

          <Section title="Timeline">
            <ol className="otimeline">
              {!(order.timeline ?? []).some((e) => e.action === "order.create") && (
                <li>
                  <span className="mono">{utcStamp(order.placed_at)}</span> Order placed · {customerLine(order)}
                </li>
              )}
              {(order.timeline ?? []).map((e) => (
                <li key={e.id}>
                  <span className="mono">{utcStamp(e.created_at)}</span> {eventLine(e)} <span className="osub">· {e.actor}</span>
                  {e.note && <div className="osub opre">“{e.note}”</div>}
                </li>
              ))}
            </ol>
          </Section>
        </div>
      </aside>
    </>
  );
}
