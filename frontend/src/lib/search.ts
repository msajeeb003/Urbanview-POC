/**
 * S2 "Find a Location": what the topbar search offers for a query. Pure (no React, no fetch), so
 * the rules are unit-tested (`search.test.ts`); `components/shell/search-box.tsx` renders them.
 *
 * - **Parcel reference**: `1042`, `1042/3`, `#1042`, `parcel 1042/3`, optionally followed by the
 *   cadastral municipality (`1042/3 Podgorica II`, `1042, pod 2`). The KO is mandatory (parcel
 *   numbers repeat across KOs): without a unique KO the row asks for one and the KO picker shows
 *   the candidates. UrbanView's internal Parcel ID is never a search input.
 * - **Zones**: names from `/v1/zones`, matched by word prefix, diacritics ignored.
 * - **Addresses**: geocoder hits, placed in their zone with the zone outlines ("Address ·
 *   Centar"); a hit in no zone or in a zone without an adopted plan gets ⚠ "Outside coverage".
 *   `/v1/locate` stays the authority once a hit is picked.
 * - **Recent searches**: the last five picks, in this browser only (`localStorage`).
 *
 * Icons are the wireframe's: ⌂ address, # parcel reference, ▤ zone, ⚠ outside coverage.
 */
import type { GeocodeKind, GeocodeResult, ZoneIndexEntry } from "./api/types";
import { parseParcelNumber } from "./format";
import { readJson, safeLocalStorage, writeJson, type KeyValueStore } from "./storage";
import type { LngLat } from "./store";

export type SearchIcon = "⌂" | "#" | "▤" | "⚠";

export interface ParcelRef {
  ko: string;
  number: string;
  sub: string | null;
}

/** What a zone suggestion (or a recent zone search) needs to act without the index. */
export interface ZoneRef {
  id: number;
  name: string;
  covered: boolean;
  bbox: [number, number, number, number];
}

export type SearchAction =
  | { type: "address"; point: LngLat }
  | { type: "zone"; zone: ZoneRef }
  | { type: "parcel"; ref: ParcelRef }
  /** A parcel number without a unique KO: the row hands over to the KO picker. */
  | { type: "choose-ko" };

export interface SearchItem {
  key: string;
  icon: SearchIcon;
  title: string;
  sub: string;
  action: SearchAction;
}

export interface ParcelQuery {
  number: string;
  sub: string | null;
  /** `1042/3` */
  label: string;
  /** KO candidates for the typed text, best first (all of the profile's when none was typed). */
  kos: string[];
  /** The KO when the query settles it (a unique match, or a single-KO municipality). */
  ko: string | null;
}

export const NO_MATCH_TEXT = "No match. The client will supply available data locations.";
export const OUTSIDE_COVERAGE_SUB = "Outside coverage · no adopted plan";

/** The rows' words in the shell's language (`{ref}` / `{ko}` placeholders); the mock's English by default. */
export interface SearchWords {
  parcel: string;
  cadastralRef: string;
  chooseKo: string;
  zone: string;
  outside: string;
  kinds: Record<GeocodeKind, string>;
}

export const EN_WORDS: SearchWords = {
  parcel: "Parcel #{ref}",
  cadastralRef: "Cadastral ref · {ko}",
  chooseKo: "Cadastral ref · choose the cadastral municipality",
  zone: "Zone",
  outside: OUTSIDE_COVERAGE_SUB,
  kinds: { address: "Address", street: "Street", place: "Place", poi: "Place", other: "Location" },
};

const fill = (text: string, vars: Record<string, string>) => text.replace(/\{(\w+)\}/g, (m, k: string) => vars[k] ?? m);

/** Lower case, no diacritics (đ → d), single spaces. */
export function normalize(text: string): string {
  return text
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/đ/gi, "d")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}/]+/gu, " ")
    .trim();
}

const ROMAN = ["", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"];

/** `2` also reads as `ii`: KO names use Roman numerals, visitors often type digits. */
function tokenVariants(token: string): string[] {
  const n = /^\d+$/.test(token) ? Number(token) : 0;
  return n >= 1 && n < ROMAN.length ? [token, ROMAN[n]] : [token];
}

/**
 * KO candidates for typed text: every typed word starts a word of the KO name, in order
 * (`pod 2` → Podgorica II and Podgorica III, since `2` reads `ii`, a prefix of `iii`). The KO is
 * settled when exactly one name ends with the typed words and its last word equals the last
 * typed word (`pod 2` → Podgorica II), or when a single name matches. A leading `KO` / `k.o.` is
 * ignored. Null = the text names no KO.
 */
export function matchKos(text: string, kos: readonly string[]): { kos: string[]; ko: string | null } | null {
  const typed = normalize(text)
    .replace(/^(?:ko|k o)(?:\s+|$)/, "")
    .split(" ")
    .filter(Boolean);
  if (typed.length === 0) return { kos: [...kos], ko: kos.length === 1 ? kos[0] : null };
  const exact: string[] = [];
  const prefix: string[] = [];
  for (const ko of kos) {
    const words = normalize(ko).split(" ");
    let matched = false;
    let tail = false;
    for (let start = 0; start + typed.length <= words.length && !matched; start++) {
      matched = typed.every((t, i) => tokenVariants(t).some((v) => words[start + i].startsWith(v)));
      if (matched) {
        const last = typed[typed.length - 1];
        tail = start + typed.length === words.length && tokenVariants(last).includes(words[words.length - 1]);
      }
    }
    if (matched) (tail ? exact : prefix).push(ko);
  }
  const all = [...exact, ...prefix];
  if (all.length === 0) return null;
  return { kos: all, ko: exact.length === 1 ? exact[0] : all.length === 1 ? all[0] : null };
}

const PARCEL_QUERY = /^\s*((?:parcel\s*|parcela\s*)?#?\s*\d{1,6}(?:\s*\/\s*\d{1,4})?)(?:(?:\s*[,;·]\s*|\s+)(.+?))?\s*$/i;

/**
 * A parcel reference with an optional KO after it. Text after the number that names no KO makes
 * it something else (`12 Bulevar …` is an address), so this answers null.
 */
export function parseParcelQuery(input: string, kos: readonly string[]): ParcelQuery | null {
  const m = PARCEL_QUERY.exec(input);
  if (!m) return null;
  const ref = parseParcelNumber(m[1]);
  if (!ref) return null;
  const match = matchKos(m[2] ?? "", kos);
  if (!match || match.kos.length === 0) return null;
  return {
    ...ref,
    label: `${ref.number}${ref.sub ? `/${ref.sub}` : ""}`,
    kos: match.kos,
    ko: match.ko,
  };
}

export function parcelTitle(ref: { number: string; sub: string | null }, words: SearchWords = EN_WORDS): string {
  return fill(words.parcel, { ref: `${ref.number}${ref.sub ? `/${ref.sub}` : ""}` });
}

export function parcelItem(parcel: ParcelQuery, words: SearchWords = EN_WORDS): SearchItem {
  const title = parcelTitle(parcel, words);
  if (parcel.ko) {
    const ref = { ko: parcel.ko, number: parcel.number, sub: parcel.sub };
    return {
      key: `parcel:${parcel.ko}:${parcel.label}`,
      icon: "#",
      title,
      sub: fill(words.cadastralRef, { ko: parcel.ko }),
      action: { type: "parcel", ref },
    };
  }
  return {
    key: `parcel:?:${parcel.label}`,
    icon: "#",
    title,
    sub: words.chooseKo,
    action: { type: "choose-ko" },
  };
}

/** Zones whose name contains every typed word as a word prefix; names starting with the query first. */
export function matchZones(query: string, zones: readonly ZoneIndexEntry[], limit = 3): ZoneIndexEntry[] {
  const q = normalize(query);
  if (q.length < 2) return [];
  const typed = q.split(" ");
  const scored: { zone: ZoneIndexEntry; rank: number }[] = [];
  for (const zone of zones) {
    const name = normalize(zone.name);
    const words = name.split(" ");
    if (!typed.every((t) => words.some((w) => w.startsWith(t)))) continue;
    scored.push({ zone, rank: name.startsWith(q) ? 0 : 1 });
  }
  scored.sort((a, b) => a.rank - b.rank || a.zone.name.localeCompare(b.zone.name));
  return scored.slice(0, limit).map((s) => s.zone);
}

export function zoneRef(zone: ZoneIndexEntry): ZoneRef {
  return { id: zone.id, name: zone.name, covered: zone.covered, bbox: [...zone.bbox] as ZoneRef["bbox"] };
}

export function zoneItem(zone: ZoneIndexEntry | ZoneRef, words: SearchWords = EN_WORDS): SearchItem {
  const ref = "geometry" in zone ? zoneRef(zone) : zone;
  return {
    key: `zone:${ref.id}`,
    icon: ref.covered ? "▤" : "⚠",
    title: ref.name,
    sub: ref.covered ? words.zone : `${words.zone} · ${words.outside.toLowerCase()}`,
    action: { type: "zone", zone: ref },
  };
}

type Ring = number[][];

function inRing(point: LngLat, ring: Ring): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > point.lat !== yj > point.lat && point.lng < ((xj - xi) * (point.lat - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

/** Point in a GeoJSON Polygon / MultiPolygon (outer ring in, holes out). */
export function pointInGeometry(point: LngLat, geometry: { type?: unknown; coordinates?: unknown }): boolean {
  const polygons =
    geometry.type === "MultiPolygon"
      ? (geometry.coordinates as Ring[][])
      : geometry.type === "Polygon"
        ? [geometry.coordinates as Ring[]]
        : [];
  return polygons.some(
    (rings) => rings.length > 0 && inRing(point, rings[0]) && !rings.slice(1).some((hole) => inRing(point, hole)),
  );
}

/** The zone containing a point (bbox first, then the simplified outline). */
export function zoneAt(point: LngLat, zones: readonly ZoneIndexEntry[]): ZoneIndexEntry | null {
  for (const zone of zones) {
    const [w, s, e, n] = zone.bbox;
    if (point.lng < w || point.lng > e || point.lat < s || point.lat > n) continue;
    if (pointInGeometry(point, zone.geometry)) return zone;
  }
  return null;
}

/** A geocoder hit as a row; `zones` undefined = the index is not loaded (no coverage hint then). */
export function addressItem(
  hit: GeocodeResult,
  zones: readonly ZoneIndexEntry[] | undefined,
  words: SearchWords = EN_WORDS,
): SearchItem {
  const point = { lat: hit.lat, lng: hit.lng };
  const base = { key: `geo:${hit.lat.toFixed(6)},${hit.lng.toFixed(6)}:${hit.label}`, title: hit.label, action: { type: "address", point } as const };
  const kind = words.kinds[hit.kind];
  if (!zones) return { ...base, icon: "⌂", sub: hit.address ? `${kind} · ${hit.address}` : kind };
  const zone = zoneAt(point, zones);
  if (!zone || !zone.covered) return { ...base, icon: "⚠", sub: words.outside };
  return { ...base, icon: "⌂", sub: `${kind} · ${zone.name}` };
}

export interface SuggestionInput {
  query: string;
  kos: readonly string[];
  zones: readonly ZoneIndexEntry[] | undefined;
  /** Geocoder hits for this very query (stale replies already dropped). */
  hits: readonly GeocodeResult[];
  words?: SearchWords;
}

/** Rows for a query: parcel reference, then zones, then addresses (duplicates collapsed). */
export function buildSuggestions({ query, kos, zones, hits, words = EN_WORDS }: SuggestionInput): {
  parcel: ParcelQuery | null;
  items: SearchItem[];
} {
  const parcel = parseParcelQuery(query, kos);
  const items: SearchItem[] = [];
  if (parcel) items.push(parcelItem(parcel, words));
  for (const zone of matchZones(query, zones ?? [])) items.push(zoneItem(zone, words));
  for (const hit of hits) items.push(addressItem(hit, zones, words));
  const seen = new Set<string>();
  return { parcel, items: items.filter((i) => !seen.has(i.key) && seen.add(i.key)) };
}

// --- recent searches ------------------------------------------------------------------------------

export const RECENT_KEY = "uv.search.recent";
export const RECENT_MAX = 5;

/** A recent search is the row that was picked (address / zone / parcel with its KO). */
export type RecentSearch = Omit<SearchItem, "action"> & {
  action: Exclude<SearchAction, { type: "choose-ko" }>;
};

function isRecent(value: unknown): value is RecentSearch {
  if (!value || typeof value !== "object") return false;
  const v = value as Partial<RecentSearch>;
  if (typeof v.key !== "string" || typeof v.title !== "string" || typeof v.sub !== "string") return false;
  if (!["⌂", "#", "▤", "⚠"].includes(v.icon as string)) return false;
  const a = v.action as Partial<SearchAction> | undefined;
  if (!a) return false;
  if (a.type === "address") {
    const p = (a as { point?: Partial<LngLat> }).point;
    return typeof p?.lat === "number" && typeof p?.lng === "number";
  }
  if (a.type === "parcel") {
    const r = (a as { ref?: Partial<ParcelRef> }).ref;
    return typeof r?.ko === "string" && typeof r?.number === "string" && (r.sub === null || typeof r.sub === "string");
  }
  if (a.type === "zone") {
    const z = (a as { zone?: Partial<ZoneRef> }).zone;
    return typeof z?.id === "number" && typeof z?.name === "string" && Array.isArray(z?.bbox) && z.bbox.length === 4;
  }
  return false;
}

export function readRecent(store: KeyValueStore = safeLocalStorage): RecentSearch[] {
  const list = readJson<unknown>(store, RECENT_KEY);
  return Array.isArray(list) ? list.filter(isRecent).slice(0, RECENT_MAX) : [];
}

/** Put a pick first (dropping an earlier copy), keep five, save; returns the new list. */
export function pushRecent(item: RecentSearch, store: KeyValueStore = safeLocalStorage): RecentSearch[] {
  const next = [item, ...readRecent(store).filter((r) => r.key !== item.key)].slice(0, RECENT_MAX);
  writeJson(store, RECENT_KEY, next);
  return next;
}
