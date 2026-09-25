"use client";

/**
 * "Choose your access" (wireframe `openUpgrade`, wide modal): three plan cards — Per-report
 * €100–200/site (gold "Order a report" → the order flow), Market data €29/mo (gold "Subscribe"),
 * AI unlimited €19/mo (primary "Subscribe") — with the plan that unlocks what the visitor clicked
 * tagged "Unlocks this". Opened by the market section's "Unlock →" and by the paid-locked price
 * heatmap card.
 *
 * The pilot sells no subscription: "Subscribe" records the intent (`market_data_interest` or
 * `ai_interest` with the ids the opener passes) and, for market data, turns the pilot's
 * entitlement on (`setMarketUnlocked`), as the mock's `subscribe('market')` does. No checkout,
 * no card fields.
 */
import { useTrack } from "@/lib/analytics/react";
import type { EventProperties } from "@/lib/analytics/tracker";
import { requestOrder } from "@/lib/order";
import { useShell } from "@/lib/store";

import { Cta } from "../ui/cta";
import { ModalHead } from "../ui/modal";

export const ACCESS_LABEL = "Choose your access";
/** Provisional copy: the assistant is a shell in the pilot, so its "subscription" is noted only. */
const AI_NOTED = "Noted — unlimited AI access is coming soon.";

const Check = () => (
  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden>
    <path d="M2 7l3 3 7-8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

function Bullets({ items }: { items: string[] }) {
  return (
    <ul>
      {items.map((t) => (
        <li key={t}>
          <Check />
          {t}
        </li>
      ))}
    </ul>
  );
}

export type AccessFocus = "market" | "ai";

export function AccessModal({
  focus,
  context = {},
}: {
  focus: AccessFocus;
  /** Ids of where the modal was opened from (parcel, layer), added to the intent events. */
  context?: EventProperties;
}) {
  const track = useTrack();
  const closeModal = useShell((s) => s.closeModal);
  const showToast = useShell((s) => s.showToast);
  const setMarketUnlocked = useShell((s) => s.setMarketUnlocked);

  const subscribeMarket = () => {
    track("market_data_interest", { ...context, trigger: "subscribe", plan: "market" });
    setMarketUnlocked(true);
    closeModal();
    showToast("Market data unlocked");
  };
  const subscribeAi = () => {
    track("ai_interest", { ...context, trigger: "subscribe", plan: "ai" });
    closeModal();
    showToast(AI_NOTED);
  };

  return (
    <>
      <ModalHead
        icon={
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden>
            <path d="M4 16l6-12 6 12M7 12h6" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
          </svg>
        }
        iconStyle={{ background: "var(--ink)", color: "var(--paper)" }}
        eyebrow="Three ways to go deeper"
        title="Choose your access"
        lead="Planning parameters and exploring the city are always free. These unlock the value layers."
      />
      <div className="mbody">
        <div className="plans">
          <div className="plan">
            <h3>Per-report</h3>
            <div className="pr">
              €100–200<small>/site</small>
            </div>
            <div className="pd">A one-off, expert-produced feasibility study for a single parcel.</div>
            <Bullets items={["Human expert analysis", "Risk & scenarios", "Delivered by email", "No subscription"]} />
            <Cta
              variant="gold"
              style={{ height: 38, marginTop: 12 }}
              onClick={() => {
                closeModal();
                requestOrder("access");
              }}
            >
              Order a report
            </Cta>
          </div>
          <div className={focus === "market" ? "plan feat" : "plan"}>
            {focus === "market" && <span className="ptag">Unlocks this</span>}
            <h3>Market data</h3>
            <div className="pr">
              €29<small>/mo</small>
            </div>
            <div className="pd">Land value, build cost, market value, profit &amp; ROI — on every parcel.</div>
            <Bullets items={["All financial figures", "Editable assumptions", "Low/expected/high ranges", "Unlimited parcels"]} />
            <Cta variant="gold" style={{ height: 38, marginTop: 12 }} onClick={subscribeMarket}>
              Subscribe
            </Cta>
          </div>
          <div className={focus === "ai" ? "plan feat" : "plan"}>
            {focus === "ai" && <span className="ptag">Unlocks this</span>}
            <h3>AI unlimited</h3>
            <div className="pr">
              €19<small>/mo</small>
            </div>
            <div className="pd">
              Unlimited access to the assistant UrbanView trains on its own planning corpus — not a general chatbot.
            </div>
            <Bullets
              items={[
                "Trained on Montenegrin planning documents",
                "Answers cite the source PDF and article",
                "Knows our analysis methodology",
                "Reads the calculation engine, never invents figures",
                "Unlimited questions",
              ]}
            />
            <Cta variant="primary" style={{ height: 38, marginTop: 12 }} onClick={subscribeAi}>
              Subscribe
            </Cta>
          </div>
        </div>
        <p style={{ fontSize: 11, color: "var(--ink-2)", textAlign: "center", marginTop: 14, lineHeight: 1.5 }}>
          Subscriptions require a free account. Three revenue channels are being tested in the pilot to see which delivers
          the most value — this is a mockup, no charge.
        </p>
      </div>
    </>
  );
}
