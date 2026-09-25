"use client";

/**
 * The public order page (`/orders/<reference>`, the link of the confirmation and of the order
 * e-mails): `GET /v1/orders/{reference}/status` only — status, location and turnaround, never
 * personal data, no login. The wireframe has no such screen; it is built from its parts: the
 * topbar with the logo, a card in the modal's style (`.mhead` with the paid icon, `.mbody`,
 * `.mfoot`), the `.orderref` chip, the steps as `.payline`s with the dot + word status labels, and
 * the `.ordersum` rows. The status is read again on focus and with "Check again", so a payment
 * staff record shows up when the visitor comes back.
 */
import { ApiError } from "@/lib/api/client";
import { useOrderStatus } from "@/lib/api/hooks";
import type { OrderStatusPublic } from "@/lib/api/types";
import { formatDate } from "@/lib/format";
import { turnaroundText } from "@/lib/pricing";
import { cn } from "@/lib/utils";

type Status = OrderStatusPublic["status"];

const STEPS: { status: Status; label: string }[] = [
  { status: "pending_payment", label: "Order placed" },
  { status: "paid", label: "Payment received" },
  { status: "in_progress", label: "Expert at work" },
  { status: "delivered", label: "Report delivered by email" },
];

// provisional copy (not in the wireframe)
const LEAD: Record<Status, string> = {
  pending_payment:
    "We are waiting for your bank transfer. Work starts once it arrives; the payment instructions are in your confirmation email.",
  paid: "Your payment has arrived. An expert will start on your site & feasibility analysis shortly.",
  in_progress: "An expert is preparing your site & feasibility analysis.",
  delivered: "Your analysis has been delivered — check your email for the download link.",
  refunded: "This order was refunded.",
};

const IconCard = () => (
  <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden>
    <path d="M3 4h14v12H3z" stroke="currentColor" strokeWidth="1.4" />
    <path d="M3 8h14M6 12h4" stroke="currentColor" strokeWidth="1.4" />
  </svg>
);

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

function Card({ title, lead, children, foot }: { title: string; lead: string; children?: React.ReactNode; foot: React.ReactNode }) {
  return (
    <section className="ordercard" aria-labelledby="order-title">
      <div className="mhead">
        <div className="mi" style={{ background: "var(--paid-tint)", color: "var(--paid)" }}>
          <IconCard />
        </div>
        <div className="mt">
          <div className="meyebrow" style={{ color: "var(--paid)" }}>
            Expert analysis · order status
          </div>
          <h2 id="order-title">{title}</h2>
          <p>{lead}</p>
        </div>
      </div>
      {children && <div className="mbody">{children}</div>}
      <div className="mfoot">{foot}</div>
    </section>
  );
}

const BackToMap = () => (
  // a full page load: the map starts fresh, as from any link
  // eslint-disable-next-line @next/next/no-html-link-for-pages
  <a className="cta ghost" href="/" style={{ width: "auto", padding: "0 18px" }}>
    ← Back to the map
  </a>
);

/**
 * Each step is an event the status reaches: reached steps in brand (the first and the latest with
 * their dates), the one the order waits for in gold ("next"), later ones muted.
 */
function Steps({ order }: { order: OrderStatusPublic }) {
  const at = STEPS.findIndex((s) => s.status === order.status);
  return (
    <div className="paysummary">
      <ol className="ordersteps">
        {STEPS.map((step, i) => {
          const state = i <= at ? "done" : i === at + 1 ? "next" : "later";
          const date = i === 0 ? order.placed_at : i === at ? order.status_changed_at : null;
          return (
            <li key={step.status} className="payline" aria-current={i === at ? "step" : undefined}>
              <span className={cn("st", state === "done" ? "ok" : state === "next" ? "pend" : "rev")}>{step.label}</span>
              <span className="mono">{date ? formatDate(date) : state === "done" ? "done" : state === "next" ? "next" : "—"}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export function OrderStatusPage({ reference }: { reference: string }) {
  const query = useOrderStatus(reference);
  const order = query.data;

  let card: React.ReactNode;
  if (order) {
    const turnaround = turnaroundText(order.turnaround.business_days);
    const where = [order.location.document_name, order.location.zone_name].filter(Boolean).join(" · ");
    card = (
      <Card
        title={capital(order.status_label_en)}
        lead={LEAD[order.status]}
        foot={
          <>
            <BackToMap />
            <button
              type="button"
              className="cta primary"
              style={{ width: "auto", padding: "0 18px" }}
              disabled={query.isFetching}
              onClick={() => void query.refetch()}
            >
              {query.isFetching ? "Checking…" : "Check again"}
            </button>
          </>
        }
      >
        <div className="orderref">
          {order.reference} · {order.location.parcel_label}
        </div>
        {order.status !== "refunded" && (
          <>
            <div className="fieldlab paylab">Progress</div>
            <Steps order={order} />
          </>
        )}
        <div className="fieldlab paylab">Order</div>
        <div className="ordersum">
          <div className="osrow">
            <span className="osl">Location{where && <em>{where}</em>}</span>
            <span className="mono">{order.location.parcel_label}</span>
          </div>
          <div className="osrow">
            <span className="osl">Placed</span>
            <span className="mono">{formatDate(order.placed_at)}</span>
          </div>
          <div className="osrow">
            <span className="osl">Last update</span>
            <span className="mono">{formatDate(order.status_changed_at)}</span>
          </div>
          <div className="osrow deliv">
            <span className="osl">
              Expected delivery<em>{order.turnaround.note_en}</em>
            </span>
            <span className="mono">{turnaround}</span>
          </div>
        </div>
      </Card>
    );
  } else if (query.error instanceof ApiError && query.error.isNotFound) {
    card = (
      <Card
        title="Order not found"
        lead={`No order has the reference ${reference}. Check the link in your confirmation email.`}
        foot={<BackToMap />}
      />
    );
  } else if (query.isError) {
    card = (
      <Card
        title="Order status unavailable"
        lead="The status could not be loaded just now."
        foot={
          <>
            <BackToMap />
            <button type="button" className="cta primary" style={{ width: "auto", padding: "0 18px" }} onClick={() => void query.refetch()}>
              Try again
            </button>
          </>
        }
      />
    );
  } else {
    card = <Card title="Loading…" lead={`Order ${reference}`} foot={<BackToMap />} />;
  }

  return (
    <div className="app">
      <header className="topbar">
        {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
        <a className="brand" href="/" aria-label="UrbanView — back to the map">
          {/* eslint-disable-next-line @next/next/no-img-element -- SVG logo, sized by the stylesheet */}
          <img className="logo" src="/brand/UrbanView_logo.svg" alt="UrbanView" />
        </a>
      </header>
      <main className="orderpage">{card}</main>
    </div>
  );
}
