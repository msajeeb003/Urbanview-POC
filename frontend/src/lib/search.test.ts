import { describe, expect, it } from "vitest";

import type { GeocodeResult, ZoneIndexEntry } from "./api/types";
import {
  NO_MATCH_TEXT,
  OUTSIDE_COVERAGE_SUB,
  RECENT_KEY,
  addressItem,
  buildSuggestions,
  matchKos,
  matchZones,
  normalize,
  parseParcelQuery,
  pointInGeometry,
  pushRecent,
  readRecent,
  zoneAt,
  zoneItem,
  type RecentSearch,
} from "./search";
import { memoryStore, writeJson } from "./storage";

const KOS = ["Podgorica I", "Podgorica II", "Podgorica III"];

function square(w: number, s: number, e: number, n: number) {
  return { type: "MultiPolygon", coordinates: [[[[w, s], [e, s], [e, n], [w, n], [w, s]]]] };
}

function zone(id: number, name: string, covered: boolean, bbox: [number, number, number, number]): ZoneIndexEntry {
  return {
    id,
    name,
    zone_type: "res",
    covered,
    bbox,
    centroid: { lng: (bbox[0] + bbox[2]) / 2, lat: (bbox[1] + bbox[3]) / 2 },
    geometry: square(...bbox),
  };
}

const ZONES = [
  zone(1, "Centar", true, [19.25, 42.435, 19.285, 42.45]),
  zone(2, "Stari Aerodrom", true, [19.285, 42.425, 19.31, 42.44]),
  zone(3, "Konik", false, [19.29, 42.44, 19.32, 42.46]),
  zone(4, "Zabjelo", true, [19.26, 42.42, 19.285, 42.435]),
];

function hit(label: string, lat: number, lng: number, kind: GeocodeResult["kind"] = "address"): GeocodeResult {
  return { label, address: `${label}, Podgorica`, lat, lng, kind };
}

describe("normalize", () => {
  it("drops case, diacritics and punctuation", () => {
    expect(normalize("  Bulevar Save Kovačevića, 12 ")).toBe("bulevar save kovacevica 12");
    expect(normalize("Đečevića")).toBe("decevica");
    expect(normalize("1042/3")).toBe("1042/3");
  });
});

describe("matchKos", () => {
  it("offers every KO when none is typed, settles a single-KO municipality", () => {
    expect(matchKos("", KOS)).toEqual({ kos: KOS, ko: null });
    expect(matchKos("", ["Podgorica I"])).toEqual({ kos: ["Podgorica I"], ko: "Podgorica I" });
  });
  it.each([
    ["Podgorica II", "Podgorica II"],
    ["podgorica ii", "Podgorica II"],
    ["pod 2", "Podgorica II"],
    ["ii", "Podgorica II"],
    ["KO Podgorica III", "Podgorica III"],
    ["3", "Podgorica III"],
  ])("%s settles %s", (text, ko) => {
    const match = matchKos(text, KOS)!;
    expect(match.ko).toBe(ko);
    expect(match.kos[0]).toBe(ko);
  });
  it("keeps the candidates open for a partial name", () => {
    expect(matchKos("podg", KOS)).toEqual({ kos: KOS, ko: null });
  });
  it("answers null for text that names no KO", () => {
    expect(matchKos("Bulevar", KOS)).toBeNull();
  });
});

describe("parseParcelQuery", () => {
  it.each([
    ["1042", "1042", null, null],
    ["1042/3", "1042", "3", null],
    [" #1042 / 3 ", "1042", "3", null],
    ["parcel 77", "77", null, null],
    ["1042/3 Podgorica II", "1042", "3", "Podgorica II"],
    ["1042, pod 3", "1042", null, "Podgorica III"],
    ["Parcel #1042/3 · Podgorica I", "1042", "3", "Podgorica I"],
  ])("%s", (input, number, sub, ko) => {
    const q = parseParcelQuery(input, KOS)!;
    expect(q).not.toBeNull();
    expect([q.number, q.sub, q.ko]).toEqual([number, sub, ko]);
    expect(q.label).toBe(sub ? `${number}/${sub}` : number);
  });
  it.each(["Bulevar Save Kovačevića 12", "12 Bulevar", "12a", "1042/", ""])("rejects %s", (input) => {
    expect(parseParcelQuery(input, KOS)).toBeNull();
  });
});

describe("zones", () => {
  it("matches names by word prefix, diacritics ignored, starts first", () => {
    expect(matchZones("aero", ZONES).map((z) => z.name)).toEqual(["Stari Aerodrom"]);
    expect(matchZones("stari aer", ZONES).map((z) => z.name)).toEqual(["Stari Aerodrom"]);
    expect(matchZones("k", ZONES)).toEqual([]); // one letter is not a query
    expect(matchZones("zab", ZONES).map((z) => z.id)).toEqual([4]);
  });
  it("marks a zone without an adopted plan as outside coverage", () => {
    expect(zoneItem(ZONES[0])).toMatchObject({ icon: "▤", title: "Centar", sub: "Zone" });
    expect(zoneItem(ZONES[2])).toMatchObject({ icon: "⚠", sub: "Zone · outside coverage · no adopted plan" });
    expect(zoneItem(ZONES[2]).action).toEqual({
      type: "zone",
      zone: { id: 3, name: "Konik", covered: false, bbox: [19.29, 42.44, 19.32, 42.46] },
    });
  });
  it("places points in polygons, holes excluded", () => {
    const withHole = {
      type: "Polygon",
      coordinates: [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
      ],
    };
    expect(pointInGeometry({ lng: 2, lat: 2 }, withHole)).toBe(true);
    expect(pointInGeometry({ lng: 5, lat: 5 }, withHole)).toBe(false);
    expect(pointInGeometry({ lng: 12, lat: 5 }, withHole)).toBe(false);
    expect(zoneAt({ lng: 19.27, lat: 42.44 }, ZONES)?.name).toBe("Centar");
    expect(zoneAt({ lng: 19.4, lat: 42.44 }, ZONES)).toBeNull();
  });
});

describe("addressItem", () => {
  it("names the zone of a covered hit (wireframe 'Address · Centar')", () => {
    const item = addressItem(hit("Bulevar Save Kovačevića 12", 42.44, 19.27), ZONES);
    expect(item).toMatchObject({ icon: "⌂", sub: "Address · Centar" });
    expect(item.action).toEqual({ type: "address", point: { lat: 42.44, lng: 19.27 } });
    expect(addressItem(hit("Ulica Slobode", 42.43, 19.3, "street"), ZONES).sub).toBe("Street · Stari Aerodrom");
  });
  it("flags hits in an uncovered zone or in no zone", () => {
    expect(addressItem(hit("Konik bb", 42.45, 19.315), ZONES)).toMatchObject({ icon: "⚠", sub: OUTSIDE_COVERAGE_SUB });
    expect(addressItem(hit("Industrijska zona bb", 42.47, 19.4), ZONES)).toMatchObject({ icon: "⚠" });
  });
  it("falls back to the geocoder's address without the zone index", () => {
    expect(addressItem(hit("Njegoševa 5", 42.44, 19.27), undefined)).toMatchObject({
      icon: "⌂",
      sub: "Address · Njegoševa 5, Podgorica",
    });
  });
});

describe("buildSuggestions", () => {
  it("puts the parcel reference first, asking for the KO when it is not settled", () => {
    const { parcel, items } = buildSuggestions({ query: "1042/3", kos: KOS, zones: ZONES, hits: [] });
    expect(parcel?.kos).toEqual(KOS);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      icon: "#",
      title: "Parcel #1042/3",
      sub: "Cadastral ref · choose the cadastral municipality",
      action: { type: "choose-ko" },
    });
  });
  it("resolves a parcel reference with its KO", () => {
    const { items } = buildSuggestions({ query: "1042 Podgorica I", kos: KOS, zones: ZONES, hits: [] });
    expect(items[0]).toMatchObject({
      title: "Parcel #1042",
      sub: "Cadastral ref · Podgorica I",
      action: { type: "parcel", ref: { ko: "Podgorica I", number: "1042", sub: null } },
    });
  });
  it("lists zones before addresses and collapses duplicates", () => {
    const h = hit("Centar", 42.44, 19.27, "place");
    const { parcel, items } = buildSuggestions({ query: "Centar", kos: KOS, zones: ZONES, hits: [h, h] });
    expect(parcel).toBeNull();
    expect(items.map((i) => i.icon)).toEqual(["▤", "⌂"]);
  });
  it("has no rows for text nothing matches (the box shows the wireframe's copy)", () => {
    expect(buildSuggestions({ query: "xyzzy", kos: KOS, zones: ZONES, hits: [] }).items).toEqual([]);
    expect(NO_MATCH_TEXT).toBe("No match. The client will supply available data locations.");
  });
});

describe("recent searches", () => {
  const entry = (n: number): RecentSearch => ({
    key: `geo:${n}`,
    icon: "⌂",
    title: `Street ${n}`,
    sub: "Address · Centar",
    action: { type: "address", point: { lat: 42.44, lng: 19.27 } },
  });

  it("keeps the last five, newest first, without duplicates", () => {
    const store = memoryStore();
    for (let n = 1; n <= 7; n++) pushRecent(entry(n), store);
    pushRecent(entry(5), store);
    expect(readRecent(store).map((r) => r.key)).toEqual(["geo:5", "geo:7", "geo:6", "geo:4", "geo:3"]);
  });

  it("round-trips parcel and zone searches", () => {
    const store = memoryStore();
    pushRecent({ ...zoneItem(ZONES[0]), action: { type: "zone", zone: { id: 1, name: "Centar", covered: true, bbox: [1, 2, 3, 4] } } }, store);
    pushRecent(
      {
        key: "parcel:Podgorica I:1042",
        icon: "#",
        title: "Parcel #1042",
        sub: "Cadastral ref · Podgorica I",
        action: { type: "parcel", ref: { ko: "Podgorica I", number: "1042", sub: null } },
      },
      store,
    );
    expect(readRecent(store).map((r) => r.action.type)).toEqual(["parcel", "zone"]);
  });

  it("ignores broken or foreign data", () => {
    const store = memoryStore();
    writeJson(store, RECENT_KEY, [{ key: "x" }, entry(1), "nope", { ...entry(2), action: { type: "choose-ko" } }]);
    expect(readRecent(store).map((r) => r.key)).toEqual(["geo:1"]);
    store.set(RECENT_KEY, "{not json");
    expect(readRecent(store)).toEqual([]);
  });
});
