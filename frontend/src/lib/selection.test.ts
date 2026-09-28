import { describe, expect, it } from "vitest";

import type { LocationResolution } from "@/lib/api/types";
import { pointCoverage, pointSelection, searchProps } from "@/lib/selection";

const urban = { id: 7, urban_parcel_number: "12", area_m2: 959.6, match: "point" } as const;

function resolution(extra: Partial<LocationResolution>): LocationResolution {
  return {
    query: { mode: "point", municipality_id: "podgorica", lat: 42.4425, lng: 19.253 },
    covered: true,
    coverage: {},
    zone: { id: 2 },
    ...extra,
  } as LocationResolution;
}

describe("pointSelection", () => {
  it("selects the cadastral parcel at the point, linked to its planned parcel", () => {
    const res = resolution({
      cadastral_parcel: { parcel_id: 1001 } as LocationResolution["cadastral_parcel"],
      urban_parcel: urban as unknown as LocationResolution["urban_parcel"],
    });
    expect(pointSelection(res, "search")).toEqual({
      kind: "feature",
      type: "cadastral",
      id: 1001,
      zoneId: 2,
      linkedUrbanId: 7,
      via: "search",
    });
  });

  it("selects the planned parcel when no cadastral parcel is there", () => {
    const res = resolution({ urban_parcel: urban as unknown as LocationResolution["urban_parcel"] });
    expect(pointSelection(res, "click")).toEqual({
      kind: "feature",
      type: "urban",
      id: 7,
      zoneId: 2,
      linkedUrbanId: null,
      via: "click",
    });
  });

  it("selects nothing when neither parcel is there", () => {
    expect(pointSelection(resolution({}), "search")).toBeNull();
  });
});

describe("searchProps", () => {
  it("says where the search landed: the point to 4 decimals and the ids found", () => {
    expect(
      searchProps("address", true, "address", { recent: true }, {
        point: { lat: 42.442512345, lng: 19.253049999 },
        urbanParcelId: 7,
        zoneId: 2,
      }),
    ).toEqual({
      search_kind: "address",
      matched: true,
      result: "address",
      recent: true,
      lat: 42.4425,
      lng: 19.253,
      urban_parcel_id: 7,
      zone_id: 2,
    });
  });

  it("carries no place keys when nothing is known, and never the query text", () => {
    expect(searchProps("parcel_number", false, "parcel")).toEqual({
      search_kind: "parcel_number",
      matched: false,
      result: "parcel",
    });
  });
});

describe("pointCoverage", () => {
  it("tells an outside-coverage hit from covered land without a parcel and from a failed lookup", () => {
    expect(pointCoverage(resolution({}), true)).toBe("covered");
    expect(pointCoverage(resolution({}), false)).toBe("no_parcel");
    expect(pointCoverage(resolution({ covered: false, zone: null }), false)).toBe("uncovered");
    expect(pointCoverage(null, false)).toBe("failed");
  });

  it("travels in search_performed only when a lookup ran", () => {
    const point = { lat: 42.4415, lng: 19.2479 };
    expect(searchProps("click", false, null, undefined, { point, coverage: "uncovered" })).toEqual({
      search_kind: "click",
      matched: false,
      lat: 42.4415,
      lng: 19.2479,
      coverage: "uncovered",
    });
    expect(searchProps("parcel_number", false, "parcel", undefined, {})).not.toHaveProperty("coverage");
  });
});
