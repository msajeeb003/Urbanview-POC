"use client";

/**
 * The geometry a reviewer decides on (the right pane of the geometry review): the batch's
 * features drawn as an SVG, north up, fitted to the pane, with the features an issue names
 * outlined in the danger colour. Hovering a feature names it (label, area, issues). Simplified by
 * the API for the preview; at most 3 000 features are drawn and the pane says when a batch has
 * more.
 */
import { useEffect, useMemo, useState } from "react";

import { issueName, projectFeatures } from "@/lib/admin/geometry";
import { geometryFeaturesAction } from "@/lib/admin/geometry-actions";
import type { GeometryFeatures } from "@/lib/api/types";

const WIDTH = 640;
const HEIGHT = 520;

export function GeometryPreview({ batchId, label }: { batchId: number; label: string }) {
  const [data, setData] = useState<GeometryFeatures | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);

  // one batch per mount: the screen keys the preview on the batch id
  useEffect(() => {
    let cancelled = false;
    void geometryFeaturesAction(batchId).then((result) => {
      if (cancelled) return;
      if (result.ok) setData(result.data);
      else setFailed(result.message);
    });
    return () => {
      cancelled = true;
    };
  }, [batchId]);

  const drawn = useMemo(() => (data ? projectFeatures(data, WIDTH, HEIGHT) : null), [data]);
  const hovered = drawn?.shapes.find((s) => s.key === hover) ?? null;
  const flagged = drawn ? drawn.shapes.filter((s) => s.issues.length).length : 0;

  if (failed) return <div className="gvnote">{failed}</div>;
  if (!data || !drawn) return <div className="gvnote">Loading the geometry…</div>;
  if (!drawn.shapes.length) return <div className="gvnote">This batch has no features to draw.</div>;

  return (
    <div className="gvpreview">
      <div className="gvbar mono">
        <span>
          {data.total.toLocaleString("en-US")} feature{data.total === 1 ? "" : "s"}
          {data.truncated && ` · the first ${drawn.shapes.length.toLocaleString("en-US")} drawn`}
        </span>
        <span className="gvlegend">
          <i className="gvkey ok" /> drawn <i className="gvkey bad" /> named by an issue ({flagged})
        </span>
      </div>
      <svg className="gvsvg" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`${label}: the staged geometry`}>
        {drawn.shapes.map((s) => (
          <path
            key={s.key}
            d={s.d}
            className={[s.issues.length ? "gvf bad" : "gvf", hover === s.key ? "on" : ""].filter(Boolean).join(" ")}
            onMouseEnter={() => setHover(s.key)}
            onMouseLeave={() => setHover((h) => (h === s.key ? null : h))}
          >
            <title>
              {s.label}
              {s.area != null ? ` · ${s.area.toLocaleString("en-US")} m²` : ""}
              {s.issues.length ? ` · ${s.issues.map(issueName).join(", ")}` : ""}
            </title>
          </path>
        ))}
      </svg>
      <div className="gvhover mono" aria-live="polite">
        {hovered
          ? `${hovered.label}${hovered.area != null ? ` · ${hovered.area.toLocaleString("en-US")} m²` : ""}${
              hovered.issues.length ? ` · ${hovered.issues.map(issueName).join(", ")}` : ""
            }`
          : "Point at a feature to name it."}
      </div>
    </div>
  );
}
