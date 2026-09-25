"use client";

/**
 * Admin view (wireframe `.admin`): a full-area overlay over the map row with the admin bar, the
 * seven tabs and "← Back to map". Tabs are placeholders in this item; each shows the card title
 * and subtitle the wireframe gives that screen.
 */
import { useShell, type AdminTab } from "@/lib/store";

import { IconAdminTitle } from "../ui/icons";

const TABS: { id: AdminTab; label: string; title: string; sub: string }[] = [
  { id: "over", label: "Overview", title: "Pipeline status", sub: "The repeatable ingestion methodology, per district" },
  { id: "review", label: "AI review queue", title: "AI extraction — review queue", sub: "100% of extracted values need expert approval before they publish" },
  { id: "rules", label: "Planning rules", title: "Planning rules", sub: "Zone parameter sets — each carries its source & verification date" },
  { id: "fin", label: "Financial assumptions", title: "Financial assumptions", sub: "Benchmarks by district — feed the deterministic engine. Sources: Realitica, Estitor, Monstat" },
  { id: "engine", label: "Calculation engine", title: "Formulas", sub: "Calculated outputs the engine derives for every parcel — extend the model over time" },
  { id: "orders", label: "Orders", title: "Expert analysis orders", sub: "Manual fulfilment queue" },
  { id: "data", label: "Data sources", title: "Data sources", sub: "Public official data — version-controlled, licence recorded" },
];

export function AdminOverlay() {
  const view = useShell((s) => s.view);
  const tab = useShell((s) => s.adminTab);
  const setTab = useShell((s) => s.setAdminTab);
  const setView = useShell((s) => s.setView);
  const on = view === "admin";

  return (
    <div className={on ? "admin on" : "admin"} id="admin" aria-hidden={!on}>
      {on && (
        <>
          <div className="adminbar">
            <div className="at">
              <IconAdminTitle /> Admin console
            </div>
            <div className="admintabs" role="tablist">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  role="tab"
                  aria-selected={tab === t.id}
                  className={tab === t.id ? "active" : undefined}
                  data-av={t.id}
                  onClick={() => setTab(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </div>
            <button type="button" className="abtn ghost" style={{ marginLeft: "auto" }} id="adminExit" onClick={() => setView("map")}>
              ← Back to map
            </button>
          </div>
          <div className="adminbody">
            {TABS.map((t) => (
              <div key={t.id} className={tab === t.id ? "adminview on" : "adminview"} id={`av-${t.id}`} role="tabpanel">
                {tab === t.id && (
                  <div className="card">
                    <div className="cardhd">
                      <div>
                        <h3>{t.title}</h3>
                        <div className="sub">{t.sub}</div>
                      </div>
                    </div>
                    <div className="review-item">
                      <div className="rv">
                        <div className="rsrc">Not connected yet.</div>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
