import { describe, expect, it } from "vitest";

import type { TilesCurrent, ZoneIndexEntry } from "../api/types";
import { DEFAULT_CHOROPLETH, DEFAULT_LAYER_STATE, hasNoData, layerById, legendGroups } from "../layers";
import { EN_WORDS, buildSuggestions, type SearchWords } from "../search";

import { STRINGS, translate, type Translate } from "./strings";

const me: Translate = (key, vars) => translate("me", key, vars);

describe("the shell's string table", () => {
  it("has every English key in Montenegrin, none empty, with the same placeholders", () => {
    const placeholders = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    expect(Object.keys(STRINGS.me).sort()).toEqual(Object.keys(STRINGS.en).sort());
    for (const [key, text] of Object.entries(STRINGS.en)) {
      const other = STRINGS.me[key as keyof typeof STRINGS.en];
      expect(other.trim(), key).not.toBe("");
      expect(placeholders(other), key).toEqual(placeholders(text));
    }
  });

  it("fills placeholders and leaves unknown ones", () => {
    expect(translate("en", "toast.centred", { name: "Podgorica" })).toBe("Centred on Podgorica");
    expect(translate("me", "ai.quota", { n: 3 })).toBe("Preostalo 3 od 3 besplatnih pitanja u ovoj sesiji");
    expect(translate("en", "search.parcel")).toBe("Parcel #{ref}");
  });
});

describe("shell text in Montenegrin", () => {
  it("names the legend groups and rows from the table", () => {
    const groups = legendGroups({
      layers: { ...DEFAULT_LAYER_STATE, owner: true },
      marketUnlocked: false,
      choropleth: DEFAULT_CHOROPLETH,
      classes: null,
      t: me,
    });
    expect(groups.map((g) => g.title)).toEqual([
      "Planski dokumenti",
      "Urbane zone",
      "Katastarske parcele",
      "Urbanističke parcele",
      "Javno vlasništvo",
    ]);
    expect(groups[1].rows.map((r) => r.label)).toContain("Stanovanje");
    // English stays the default without a translator
    expect(legendGroups({ layers: DEFAULT_LAYER_STATE, marketUnlocked: false, choropleth: DEFAULT_CHOROPLETH, classes: null })[0].title).toBe(
      "Planning documents",
    );
  });

  it("words the search rows", () => {
    const words: SearchWords = {
      parcel: me("search.parcel"),
      cadastralRef: me("search.cadastralRef"),
      chooseKo: me("search.chooseKo"),
      zone: me("search.zone"),
      outside: me("search.outside"),
      kinds: { ...EN_WORDS.kinds, address: me("search.kind.address") },
    };
    const zone = { id: 1, name: "Centar", covered: true, bbox: [19, 42, 20, 43] } as unknown as ZoneIndexEntry;
    const { items } = buildSuggestions({ query: "1042/3 Podgorica I", kos: ["Podgorica I"], zones: [zone], hits: [], words });
    expect(items[0]).toMatchObject({ title: "Parcela #1042/3", sub: "Katastarska oznaka · Podgorica I" });
    const cent = buildSuggestions({ query: "cent", kos: [], zones: [zone], hits: [], words }).items[0];
    expect(cent.sub).toBe("Zona");
  });
});

describe("the layer card's no-data note", () => {
  const tiles = (layers: { id: string; features: number }[], archive = "https://s3/x.pmtiles") =>
    ({ status: "published", archive_url: archive, layers }) as unknown as TilesCurrent;

  it("says so when the published version has nothing for all of the layer's source-layers", () => {
    expect(hasNoData(layerById("traffic"), tiles([{ id: "traffic_network", features: 0 }]))).toBe(true);
    expect(hasNoData(layerById("traffic"), tiles([]))).toBe(true);
    expect(hasNoData(layerById("zones"), tiles([{ id: "zones", features: 12 }]))).toBe(false);
  });

  it("never for the base map, an unpublished pointer or a version without an archive", () => {
    expect(hasNoData(layerById("base"), tiles([]))).toBe(false);
    expect(hasNoData(layerById("traffic"), tiles([], ""))).toBe(false);
    expect(hasNoData(layerById("traffic"), undefined)).toBe(false);
  });
});
