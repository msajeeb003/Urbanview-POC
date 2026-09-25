"use client";

/**
 * Map legend card (wireframe `.legend`, `renderLegend`): one group per drawn layer, from each
 * registry entry's `legend` (`lib/layers.ts`). Choropleth groups list the classes the API served
 * with the current tiles, coloured by the same functions the map uses, so legend and map match;
 * "No data" (hatch) appears when some cells have no value. "No overlays active" when nothing is on.
 */
import type { CSSProperties } from "react";

import { useTilesCurrent } from "@/lib/api/hooks";
import { useT } from "@/lib/i18n";
import { legendGroups, type LegendMark } from "@/lib/layers";
import { useShell } from "@/lib/store";

function MarkSwatch({ mark }: { mark: LegendMark }) {
  switch (mark.kind) {
    case "line":
      return <span className="sw line" style={{ "--lc": mark.color } as CSSProperties} />;
    case "grad":
      return (
        <span
          className="sw"
          style={{ width: 26, flex: "0 0 26px", borderRadius: 3, background: `linear-gradient(90deg,${mark.stops})` }}
        />
      );
    case "dash":
      return <span className="sw" style={{ background: "#fff", border: "1.5px dashed #B5613B" }} />;
    case "docdash":
      return <span className="sw" style={{ background: "#fff", border: "2px dashed #B3A894" }} />;
    case "blockdash":
      return <span className="sw" style={{ background: "#fff", border: "1.5px dotted #B3A894" }} />;
    case "cadsw":
      return <span className="sw" style={{ background: "#fff", border: "1.5px solid #B3A894" }} />;
    case "hatch":
      return <span className="sw sw-nodata" />;
    case "color":
      return <span className="sw" style={{ background: mark.color }} />;
  }
}

export function Legend() {
  const layers = useShell((s) => s.layers);
  const marketUnlocked = useShell((s) => s.marketUnlocked);
  const choropleth = useShell((s) => s.choropleth);
  const min = useShell((s) => s.legendMin);
  const toggle = useShell((s) => s.toggleLegend);
  const { data: tiles } = useTilesCurrent();
  const t = useT();

  const groups = legendGroups({ layers, marketUnlocked, choropleth, classes: tiles?.cell_classes, t });

  return (
    <div className={min ? "legend min" : "legend"} id="legend">
      <h4>
        {t("legend.title")}{" "}
        <button
          type="button"
          id="legmin"
          aria-label={min ? t("legend.expand") : t("legend.minimise")}
          onClick={(e) => {
            e.stopPropagation();
            toggle();
          }}
        >
          {min ? "+" : "–"}
        </button>
      </h4>
      <div className="legbody">
        {groups.length === 0 ? (
          <div className="legrow" style={{ opacity: 0.6 }}>
            {t("legend.none")}
          </div>
        ) : (
          groups.map((g) => (
            <div className="leggrp" key={g.title}>
              <div className="legh">
                {g.title}
                {g.unit && (
                  <>
                    {" "}
                    <span className="legu">{g.unit}</span>
                  </>
                )}
              </div>
              {g.rows.map((row, i) => (
                <div className="legrow" key={i}>
                  <MarkSwatch mark={row.mark} />
                  {row.label}
                </div>
              ))}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
