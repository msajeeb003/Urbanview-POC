"use client";

/**
 * Chrome over the map (wireframe `.mapchrome`): legend, outside-coverage pill, scale bar,
 * coordinates chip and the zoom tools (zoom label, +, −, reset).
 */
import { useMunicipality } from "@/lib/api/hooks";
import { formatCoords } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { useShell } from "@/lib/store";

import { IconReset, IconWarn } from "../ui/icons";

import { Legend } from "./legend";

export function MapChrome() {
  const coverWarn = useShell((s) => s.coverWarn);
  const coverReason = useShell((s) => s.coverReason);
  const coords = useShell((s) => s.coords);
  const zoomLabel = useShell((s) => s.zoomLabel);
  const scale = useShell((s) => s.scale);
  const map = useShell((s) => s.map);
  const { data: profile } = useMunicipality();
  const t = useT();
  const shown = coords ?? (profile ? { lng: profile.center[0], lat: profile.center[1] } : null);

  return (
    <div className="mapchrome">
      <Legend />
      <div className={coverWarn ? "coverwarn on" : "coverwarn"} id="coverwarn" role="status">
        <IconWarn />
        <span>{t("cover.title")}</span>{" "}
        <span className="mono">
          {coverReason === "outside_municipality" && profile ? t("cover.outside", { name: profile.name }) : t("cover.noPlan")}
        </span>
      </div>
      <div className="scalebar">
        <span className="t">{scale.label}</span>
        <div className="bar" style={{ width: scale.widthPx }} />
      </div>
      <div className="coords mono" id="coords">
        {shown ? formatCoords(shown) : " "}
      </div>
      <div className="maptools">
        <div className="zlabel mono" id="zlabel" style={{ textAlign: "center" }}>
          {zoomLabel}
        </div>
        <button type="button" className="mbtn" id="zin" aria-label={t("map.zoomIn")} onClick={() => map?.zoomIn()}>
          +
        </button>
        <button type="button" className="mbtn" id="zout" aria-label={t("map.zoomOut")} onClick={() => map?.zoomOut()}>
          −
        </button>
        <button
          type="button"
          className="mbtn"
          id="zreset"
          aria-label={t("map.reset")}
          onClick={() => {
            map?.reset();
            useShell.getState().clearSelection();
          }}
        >
          <IconReset />
        </button>
      </div>
    </div>
  );
}
