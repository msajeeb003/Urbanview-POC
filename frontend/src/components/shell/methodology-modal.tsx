"use client";

/**
 * "Our analysis methodology" (wireframe `renderMethodology`, the wide `.method` modal): a rail of
 * six steps and a pane with the step's tag, title, phase, diagram and text; Back / Next step, and
 * on the last step the gold "Order this analysis →". Copy and diagrams are the wireframe's.
 * Opened from "How we read a planning document" (step 2) and, later, "How we analyze this parcel"
 * (step 1). `onOrder` is the parcel panel's order flow; without it the last button says where
 * ordering starts.
 */
import { useState, type ReactNode } from "react";

import { useShell } from "@/lib/store";

import { Cta } from "../ui/cta";
import { ModalHead } from "../ui/modal";

export const METHODOLOGY_LABEL = "Our analysis methodology";
/** Provisional copy (not in the wireframe): ordering needs a parcel and starts from its panel. */
const PICK_A_PARCEL = "Pick a parcel on the map to order its analysis.";

interface Step {
  t: string;
  tag: string;
  phase: string;
  body: string;
  subs?: [string, string][];
  dia: ReactNode;
}

const STEPS: Step[] = [
  {
    t: "Locate the parcel",
    tag: "Research",
    phase: "Phase 1 · Research & ownership",
    body: "Searching for the specific cadastral parcel in the urban plans that also contain existing condition and planned condition. First check is if the cadastral parcel is the same geometry and area as planned one - urban parcel. During that research we check the ownership status of the parcel.",
    dia: (
      <svg width="220" height="118" viewBox="0 0 220 118" fill="none">
        <rect x="14" y="14" width="86" height="90" rx="3" stroke="#B3A894" strokeWidth="1.4" />
        <path d="M14 44h86M14 74h86M44 14v90M72 14v90" stroke="#DDD3C3" strokeWidth="1" />
        <rect x="44" y="44" width="28" height="30" fill="#B5613B" fillOpacity=".16" stroke="#B5613B" strokeWidth="1.4" />
        <path d="M150 30l40 40M150 70l40-40" stroke="#DDD3C3" strokeWidth="0" />
        <circle cx="163" cy="52" r="15" stroke="#B5613B" strokeWidth="1.6" />
        <path d="M174 63l14 14" stroke="#B5613B" strokeWidth="1.6" strokeLinecap="round" />
        <path d="M120 44h20M120 60h14" stroke="#B3A894" strokeWidth="1.2" />
      </svg>
    ),
  },
  {
    t: "Extract planning parameters",
    tag: "Research",
    phase: "Phase 1 · Read the adopted plan",
    body: "We look for parts from urban planning PDF's that contain specific information:",
    subs: [
      ["a", "Planned urban parcels - regulation and its relation to public areas"],
      [
        "b",
        "Planned urban regulation - Area envisaged for site coverage by the object, building height and number of floors, linear distance from the neighboring parcels and public areas.",
      ],
      ["c", "Planned land use within the block or area that has the same markation"],
      [
        "d",
        "Existing and planned infrastructure utilities - water, sewage, energy infrastructure, heating, access roads etc.",
      ],
    ],
    dia: (
      <svg width="200" height="128" viewBox="0 0 200 128" fill="none">
        <rect x="40" y="8" width="120" height="112" rx="4" fill="#fff" stroke="#B3A894" strokeWidth="1.4" />
        <path d="M56 30h88M56 44h88M56 58h64" stroke="#DDD3C3" strokeWidth="1.4" />
        <rect x="56" y="72" width="88" height="12" fill="#B5613B" fillOpacity=".16" stroke="#B5613B" strokeWidth="1.2" />
        <path d="M56 96h72M56 108h50" stroke="#DDD3C3" strokeWidth="1.4" />
        <circle cx="150" cy="78" r="12" fill="#fff" stroke="#B5613B" strokeWidth="1.5" />
        <path d="M145 78l3 3 6-7" stroke="#B5613B" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    ),
  },
  {
    t: "2D orthogonal projection",
    tag: "Design",
    phase: "Phase 2 · Design",
    body: "After analyzing the parcel and the referent PDF planning document, we start designing the orthogonal projection (2D view from the top) of the object to the parcel, implementing all the rules and regulations.",
    dia: (
      <svg width="210" height="120" viewBox="0 0 210 120" fill="none">
        <rect x="20" y="14" width="170" height="92" rx="3" stroke="#B3A894" strokeWidth="1.4" />
        <rect x="46" y="34" width="118" height="52" fill="#B5613B" fillOpacity=".14" stroke="#B5613B" strokeWidth="1.6" />
        <path d="M20 34h26M164 34h26M20 86h26M164 86h26" stroke="#B5613B" strokeWidth="1" strokeDasharray="3 3" />
        <path d="M46 14v20M46 86v20M164 14v20M164 86v20" stroke="#B5613B" strokeWidth="1" strokeDasharray="3 3" />
        <path d="M76 34v52M106 34v52M136 34v52M46 60h118" stroke="#DDD3C3" strokeWidth="1" />
      </svg>
    ),
  },
  {
    t: "3D fitting",
    tag: "Design",
    phase: "Phase 2 · Massing in context",
    body: "After the design of the object we start working on 3D view and we start fitting the object into the build environment.",
    dia: (
      <svg width="210" height="122" viewBox="0 0 210 122" fill="none">
        <path d="M30 92l70-24 70 24-70 24z" fill="#EDEFF2" stroke="#B3A894" strokeWidth="1.2" />
        <path d="M84 66l0-34 32-11 0 34z" fill="#B5613B" fillOpacity=".18" stroke="#B5613B" strokeWidth="1.5" />
        <path d="M84 66l32-11M84 32l32-11" stroke="#B5613B" strokeWidth="1.5" />
        <path d="M116 21l24 8 0 34-24 12" fill="#B5613B" fillOpacity=".1" stroke="#B5613B" strokeWidth="1.5" />
        <path d="M40 84l26-9 0 18-26 9z" fill="#fff" stroke="#B3A894" strokeWidth="1.2" />
        <path d="M150 84l22-8 0 16-22 8z" fill="#fff" stroke="#B3A894" strokeWidth="1.2" />
      </svg>
    ),
  },
  {
    t: "Preliminary package & feasibility",
    tag: "Deliverable",
    phase: "Phase 3 · Client package",
    body: "This step is the final. After we conclude that it is the design that should be presented to the Client, we prepare preliminary 2D plans, 3D model and comprehensive analysis of real estate development venture.",
    dia: (
      <svg width="200" height="120" viewBox="0 0 200 120" fill="none">
        <rect x="24" y="30" width="70" height="84" rx="3" fill="#fff" stroke="#B3A894" strokeWidth="1.3" transform="rotate(-6 59 72)" />
        <rect x="34" y="20" width="70" height="84" rx="3" fill="#fff" stroke="#B3A894" strokeWidth="1.3" />
        <path d="M46 40h46M46 54h46M46 68h30" stroke="#DDD3C3" strokeWidth="1.3" />
        <path d="M46 82h46v14H46z" fill="#B5613B" fillOpacity=".14" stroke="#B5613B" strokeWidth="1.1" />
        <path d="M118 96l40-14 0-30-40 14z" fill="#B5613B" fillOpacity=".16" stroke="#B5613B" strokeWidth="1.4" />
        <path d="M118 66l40-14M138 45v52" stroke="#B5613B" strokeWidth="1.2" />
      </svg>
    ),
  },
  {
    t: "Offer & design",
    tag: "Engagement",
    phase: "Phase 4 · Engagement",
    body: "If the Client decides to go for the venture, we prepare offer and start designing.",
    dia: (
      <svg width="200" height="118" viewBox="0 0 200 118" fill="none">
        <rect x="48" y="16" width="104" height="86" rx="4" fill="#fff" stroke="#B3A894" strokeWidth="1.4" />
        <path d="M64 34h72M64 48h72M64 62h48" stroke="#DDD3C3" strokeWidth="1.3" />
        <path d="M66 82c8-10 18-10 24 0 6 8 14 6 18-2" stroke="#B5613B" strokeWidth="1.8" strokeLinecap="round" />
        <circle cx="150" cy="86" r="18" fill="#B5613B" fillOpacity=".14" stroke="#B5613B" strokeWidth="1.5" />
        <path d="M143 86l5 5 9-11" stroke="#B5613B" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    ),
  },
];

const IconMethod = () => (
  <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden>
    <circle cx="4" cy="4" r="2" stroke="currentColor" strokeWidth="1.4" />
    <circle cx="4" cy="16" r="2" stroke="currentColor" strokeWidth="1.4" />
    <path d="M4 6v8M8 4h8M8 10h8M8 16h8" stroke="currentColor" strokeWidth="1.4" />
  </svg>
);

export function MethodologyModal({
  initialStep = 0,
  context = "Any parcel",
  onOrder,
}: {
  initialStep?: number;
  /** The location the steps are read for ("Parcel #1042 · Podgorica I"); the mock's default. */
  context?: string;
  onOrder?: () => void;
}) {
  const [step, setStep] = useState(Math.max(0, Math.min(STEPS.length - 1, initialStep)));
  const closeModal = useShell((s) => s.closeModal);
  const showToast = useShell((s) => s.showToast);
  const s = STEPS[step];
  const last = step === STEPS.length - 1;

  return (
    <>
      <ModalHead
        icon={<IconMethod />}
        iconStyle={{ background: "var(--brand-tint)", color: "var(--brand)" }}
        eyebrow="How we work"
        title="Our analysis methodology"
        lead={
          <>
            The step-by-step process we follow every time we analyze a parcel — for our services, or under contract to
            design the object. Context: <b>{context}</b>.
          </>
        }
      />
      <div className="method">
        <div className="mrail">
          <div className="mrail-h">Six steps</div>
          {STEPS.map((m, i) => (
            <div
              key={m.t}
              className={["mstep", i === step && "on", i < step && "done"].filter(Boolean).join(" ")}
              role="button"
              tabIndex={0}
              aria-current={i === step ? "step" : undefined}
              onClick={() => setStep(i)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setStep(i);
                }
              }}
            >
              <div className="mn">{i < step ? "✓" : i + 1}</div>
              <div className="ml">
                <b>{m.t}</b>
                <span>{m.tag}</span>
              </div>
            </div>
          ))}
        </div>
        <div className="mpane">
          <div className="peyebrow2">{s.tag}</div>
          <h2>{s.t}</h2>
          <div className="phase">{s.phase}</div>
          <div className="mdiagram">{s.dia}</div>
          <div className="mbodytext">
            {s.body}
            {s.subs && (
              <>
                <div style={{ height: 6 }} />
                {s.subs.map(([k, v]) => (
                  <span key={k} className="sub" data-k={k}>
                    {v}
                  </span>
                ))}
              </>
            )}
          </div>
          <div className="mnav">
            <span className="mcount">
              Step {step + 1} / {STEPS.length}
            </span>
            <div className="mprog">
              <i style={{ width: `${(((step + 1) / STEPS.length) * 100).toFixed(0)}%` }} />
            </div>
            {step > 0 && (
              <Cta variant="ghost" style={{ width: "auto", padding: "0 16px", height: 38 }} onClick={() => setStep(step - 1)}>
                ← Back
              </Cta>
            )}
            {last ? (
              <Cta
                variant="gold"
                style={{ width: "auto", padding: "0 18px", height: 38 }}
                onClick={() => {
                  closeModal();
                  if (onOrder) onOrder();
                  else showToast(PICK_A_PARCEL);
                }}
              >
                Order this analysis →
              </Cta>
            ) : (
              <Cta variant="primary" style={{ width: "auto", padding: "0 18px", height: 38 }} onClick={() => setStep(step + 1)}>
                Next step →
              </Cta>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
