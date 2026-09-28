"use client";

/**
 * Pieces the cadastral and urban parcel panels share: the cadastral-vs-urban comparison card
 * (wireframe `.vscard` with its mini diagram; the area mismatch is always stated, never silent),
 * the per-value source icon, the CTA stack (gold order + price, the two intent buttons, line
 * methodology) and the zone-type lookup for the eyebrow.
 */
import { useEffect, type ReactNode } from "react";

import { useTrack } from "@/lib/analytics/react";
import { useOrderPricing, useZones } from "@/lib/api/hooks";
import type { PlanningField } from "@/lib/api/types";
import { formatArea } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { ZONE_TYPES } from "@/lib/layers";
import { requestOrder } from "@/lib/order";
import type { OrderTarget } from "@/lib/order-form";
import { formatPrice, priceFor } from "@/lib/pricing";
import { useOpenSource } from "@/lib/source";
import { sourceRefText } from "@/lib/source-text";
import { useShell } from "@/lib/store";

import { targetLabel } from "../order/order-modal";
import { METHODOLOGY_LABEL, MethodologyModal } from "../shell/methodology-modal";
import { Cta } from "../ui/cta";
import { IconAsk, IconDocSmall, IconLock, IconOrder, IconSteps } from "../ui/icons";

export { formatArea };

/** `Parcel #1042/3` → the number part `1042/3`. */
export function parcelNo(number: string, sub: string | null | undefined): string {
  return sub ? `${number}/${sub}` : number;
}

/**
 * The change from the cadastral to the planned area, as the mock words it: `−30%` taken for roads
 * / public space; a planned parcel as large or larger says so instead (no silent zero).
 */
export function deltaPhrase(deltaPct: number): { delta: string | null; text: string } {
  const rounded = Math.round(deltaPct);
  const shown = rounded === 0 && deltaPct !== 0 ? Math.abs(deltaPct).toFixed(1) : String(Math.abs(rounded));
  if (deltaPct === 0) return { delta: null, text: "Same area as the cadastral parcel." };
  if (deltaPct < 0) return { delta: `−${shown}%`, text: "taken for roads / public space." };
  return { delta: `+${shown}%`, text: "larger than the cadastral parcel." };
}

function VsMini({ label }: { label: string }) {
  return (
    <svg className="vsmini" viewBox="0 0 66 52" aria-hidden>
      <rect x="4" y="4" width="58" height="44" rx="2" fill="none" stroke="#B3A894" strokeWidth="1.5" />
      <rect
        x="14"
        y="12"
        width="40"
        height="30"
        rx="2"
        fill="#B5613B"
        fillOpacity=".18"
        stroke="#B5613B"
        strokeWidth="1.4"
        strokeDasharray="3 2"
      />
      <text x="58" y="50" textAnchor="end" fontFamily="IBM Plex Mono" fontSize="5" fill="#B5613B">
        {label}
      </text>
    </svg>
  );
}

/**
 * `Cadastral 1,370.9 m² → urban 959.6 m². −30% taken for roads / public space.` Several planned
 * areas (a split cadastral parcel) are listed and the delta is taken on their total.
 */
export function AreaCompare({
  cadastralM2,
  urbanM2,
  label,
  children,
}: {
  cadastralM2: number;
  urbanM2: number[];
  /** The mini diagram's word for the planned outline (`urban` on the cadastral panel, `planned` on the urban one). */
  label: string;
  /** A line after the comparison (the calculation basis on the urban panel). */
  children?: ReactNode;
}) {
  const total = urbanM2.reduce((a, b) => a + b, 0);
  const d = deltaPhrase(cadastralM2 > 0 ? ((total - cadastralM2) / cadastralM2) * 100 : 0);
  return (
    <div className="vscard">
      <VsMini label={label} />
      <div className="txt">
        Cadastral <b>{formatArea(cadastralM2)} m²</b> → urban{" "}
        {urbanM2.map((m2, i) => (
          <span key={i}>
            {i > 0 && " + "}
            <b>{formatArea(m2)} m²</b>
          </span>
        ))}
        {urbanM2.length > 1 && ` (${urbanM2.length} urban parcels)`}.{" "}
        {d.delta && (
          <>
            <span className="vsdelta">{d.delta}</span>{" "}
          </>
        )}
        {d.text}
        {children}
      </div>
    </div>
  );
}

/** The comparison card when there is nothing to compare with (the fact is still stated). */
/** The comparison card's last line: which area the calculations use (the planned one when there is one). */
export function BasisLine({ children }: { children: ReactNode }) {
  return (
    <>
      <br />
      <span style={{ color: "var(--brand-dark)", fontWeight: 600 }}>{children}</span>
    </>
  );
}

export function AreaNote({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="vscard">
      <VsMini label={label} />
      <div className="txt">{children}</div>
    </div>
  );
}

/**
 * A value's source icon: opens the cited page of its document (`source_reference_opened`), by
 * value id when the value has one (the viewer adds its bbox and note), else by document and page.
 * Its title and accessible name are the reference itself: field, document, page and the plan's
 * note (`Max number of floors: DUP Centar – Zona C2, page 14 · table 3 – UP 12`), so two icons on
 * one row (height and floors) are told apart.
 */
export function RowSource({ fields }: { fields: (PlanningField | undefined)[] }) {
  const openSource = useOpenSource();
  const seen = new Set<string>();
  const sources = fields.flatMap((f) => {
    const s = f?.status === "stated" ? f.source : null;
    if (!f || !s) return [];
    const key = s.value_id != null ? `v${s.value_id}` : `d${s.document_id}:${s.page ?? 1}`;
    if (seen.has(key)) return [];
    seen.add(key);
    return [{ key, source: s, label: f.label_en, fallback: !!f.fallback }];
  });
  // a row without a source keeps the icon's width so its value lines up with the others
  if (sources.length === 0) return <span className="rowsrc spacer" aria-hidden />;
  return (
    <>
      {sources.map(({ key, source, label, fallback }) => {
        const ref = { label, documentName: source.document_name, page: source.page, note: source.note };
        const { title, ariaLabel } = sourceRefText({ ...ref, fallback });
        const hint = { ...ref, registryUrl: source.registry_url };
        return (
          <button
            key={key}
            type="button"
            className="rowsrc"
            title={title}
            aria-label={ariaLabel}
            onClick={() =>
              void openSource(
                source.value_id != null
                  ? { valueId: source.value_id, hint }
                  : { documentId: source.document_id, page: source.page ?? 1, hint },
              )
            }
          >
            <IconDocSmall />
          </button>
        );
      })}
    </>
  );
}

/** Zone type name for the eyebrow, from the zone index the search also uses. */
export function useZoneTypeName(zoneId: number | null | undefined): string | null {
  const { data } = useZones(zoneId != null);
  const type = data?.zones.find((z) => z.id === zoneId)?.zone_type;
  return ZONE_TYPES.find((z) => z.key === type)?.name ?? null;
}

/** Acknowledgement of "Unlock full market data" (provisional copy): the pilot locks nothing. */
export const MARKET_INTEREST_NOTED = "Thanks — noted. Market data is free for everyone during the pilot.";

/**
 * The parcel panel's button stack: gold "Order expert analysis" + price, the pilot's two intent
 * buttons (ghost "Unlock full market data" on the urban parcel panel, where Group 2 shows, and
 * ghost "Ask about this site"), line "How we analyze this parcel". The intent buttons only log
 * interest (`market_data_interest`, `ai_interest`) and say so in a toast: the POC builds no
 * subscription and no assistant, so nothing opens, unlocks or changes. The panel's parcel is
 * registered as the order target while the panel is on screen, so the methodology's last step
 * orders it too.
 */
export function ParcelCtas({
  target,
  marketIntent = false,
}: {
  /** The parcel as the panel shows it; its ids go into the intent and order events. */
  target: OrderTarget;
  /** Show "Unlock full market data" (the panel shows Group 2). */
  marketIntent?: boolean;
}) {
  const { data: pricing } = useOrderPricing();
  const openModal = useShell((s) => s.openModal);
  const setOrderTarget = useShell((s) => s.setOrderTarget);
  const showToast = useShell((s) => s.showToast);
  const track = useTrack();
  const t = useT();
  const price = priceFor(target.basisAreaM2, pricing);
  const { parcelType, ids } = target;

  // registered by value (the panels build the target on every render)
  const { parcelId, parcel, ko, basisAreaM2, calculationBasis } = target;
  const idsKey = JSON.stringify(ids);
  useEffect(() => {
    setOrderTarget({ parcelType, parcelId, parcel, ko, basisAreaM2, calculationBasis, ids: JSON.parse(idsKey) });
    return () => setOrderTarget(null);
  }, [parcelType, parcelId, parcel, ko, basisAreaM2, calculationBasis, idsKey, setOrderTarget]);

  return (
    <div className="ctastack">
      <Cta
        variant="gold"
        icon={<IconOrder />}
        trailing={price != null ? formatPrice(price, pricing?.currency) : undefined}
        onClick={() => requestOrder("panel")}
      >
        Order expert analysis
      </Cta>
      {marketIntent && (
        <Cta
          variant="ghost"
          icon={<IconLock />}
          onClick={() => {
            track("market_data_interest", { ...ids, trigger: "parcel_panel", panel_type: parcelType });
            showToast(MARKET_INTEREST_NOTED);
          }}
        >
          Unlock full market data
        </Cta>
      )}
      <Cta
        variant="ghost"
        icon={<IconAsk />}
        onClick={() => {
          track("ai_interest", { ...ids, trigger: "parcel_panel", panel_type: parcelType });
          showToast(t("ai.notYet"));
        }}
      >
        Ask about this site
      </Cta>
      <Cta
        variant="line"
        icon={<IconSteps />}
        onClick={() =>
          openModal({
            label: METHODOLOGY_LABEL,
            wide: true,
            content: (
              <MethodologyModal initialStep={0} context={targetLabel(target)} onOrder={() => requestOrder("methodology")} />
            ),
          })
        }
      >
        How we analyze this parcel
      </Cta>
    </div>
  );
}
