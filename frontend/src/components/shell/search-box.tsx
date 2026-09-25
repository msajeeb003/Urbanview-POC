"use client";

/**
 * S2 "Find a Location": the topbar search (wireframe `.searchwrap` + `.searchsug`). One input
 * takes an address, a zone name or a parcel number; the rules live in `lib/search.ts`.
 *
 * - Suggestions under the input: icon column (⌂ address, # parcel reference, ▤ zone, ⚠ outside
 *   coverage) and two lines (label, sub-label such as "Address · Centar"). Addresses come from
 *   `/v1/geocode` (debounced), zones from `/v1/zones` (matched locally, fetched on first focus).
 * - A parcel number (`1042`, `1042/3`) offers "Parcel #… · Cadastral ref" and the KO picker
 *   (the profile's cadastral municipalities, narrowed by any KO text typed after the number):
 *   the KO is mandatory before `/v1/locate/parcel` runs. A reference that matches nothing is said
 *   inline, the search stays open and the map keeps its previous selection.
 * - Picking: address → `/v1/locate` (fly, select the parcel, panel or S6); zone → frame it, its
 *   panel or S6; parcel → fly, select, panel. Every pick joins the recent searches (last five,
 *   this browser only), shown when the input is focused and empty.
 * - Empty list → "No match. The client will supply available data locations." Never an error.
 * - Keyboard: ⌘K / Ctrl+K focuses (bound in the shell), ↑ ↓ move (← → between KO chips), Enter
 *   picks (the first row when none is active), Esc closes.
 * - ≤ 760 px: a focused search opens as a full-screen sheet with a Cancel button (`overrides.css`).
 * - Analytics: `search_performed { search_kind, matched }` comes from the selection flow for
 *   picks; this box adds `matched: false` for a query left with no result (Enter or closing).
 */
import { useQueryClient } from "@tanstack/react-query";
import {
  forwardRef,
  useEffect,
  useEffectEvent,
  useId,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";

import { useTrack } from "@/lib/analytics/react";
import { api } from "@/lib/api/endpoints";
import { queryKeys, useGeocode, useMunicipality, useZones } from "@/lib/api/hooks";
import type { GeocodeResult } from "@/lib/api/types";
import { useT, type Translate } from "@/lib/i18n";
import {
  buildSuggestions,
  normalize,
  parcelTitle,
  parseParcelQuery,
  pushRecent,
  readRecent,
  type ParcelRef,
  type RecentSearch,
  type SearchItem,
  type SearchWords,
} from "@/lib/search";
import { useSelection } from "@/lib/selection";

import { IconSearch } from "../ui/icons";

/** The rows' words in the shell's language (`lib/search.ts` keeps its rules language-free). */
function searchWords(t: Translate): SearchWords {
  return {
    parcel: t("search.parcel"),
    cadastralRef: t("search.cadastralRef"),
    chooseKo: t("search.chooseKo"),
    zone: t("search.zone"),
    outside: t("search.outside"),
    kinds: {
      address: t("search.kind.address"),
      street: t("search.kind.street"),
      place: t("search.kind.place"),
      poi: t("search.kind.place"),
      other: t("search.kind.other"),
    },
  };
}

type Option = { kind: "item"; item: SearchItem; recent: boolean } | { kind: "ko"; ko: string };

const NO_HITS: readonly GeocodeResult[] = [];

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

function sameRef(a: ParcelRef | null, b: { number: string; sub: string | null } | null): boolean {
  return !!a && !!b && a.number === b.number && a.sub === b.sub;
}

function refLabel(ref: { number: string; sub: string | null }): string {
  return `${ref.number}${ref.sub ? `/${ref.sub}` : ""}`;
}

/** Options in list order; consecutive KO chips render as one picker group. */
function groupRows(options: Option[]): ({ kind: "one"; at: number } | { kind: "ko"; at: number[] })[] {
  const rows: ({ kind: "one"; at: number } | { kind: "ko"; at: number[] })[] = [];
  options.forEach((option, i) => {
    const last = rows[rows.length - 1];
    if (option.kind !== "ko") rows.push({ kind: "one", at: i });
    else if (last?.kind === "ko") last.at.push(i);
    else rows.push({ kind: "ko", at: [i] });
  });
  return rows;
}

export const SearchBox = forwardRef<HTMLInputElement>(function SearchBox(_, ref) {
  const inputRef = useRef<HTMLInputElement>(null);
  useImperativeHandle(ref, () => inputRef.current as HTMLInputElement);
  const wrapRef = useRef<HTMLDivElement>(null);
  const listId = useId();

  const [value, setValue] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  /** Label of the row just picked: shown in the input, never searched again. */
  const [picked, setPicked] = useState<string | null>(null);
  /** The zone index is fetched once the search has been opened. */
  const [touched, setTouched] = useState(false);
  const [recent, setRecent] = useState<RecentSearch[]>([]);
  /** Parcel reference being looked up, and the last one that matched nothing. */
  const [pending, setPending] = useState<ParcelRef | null>(null);
  const [notFound, setNotFound] = useState<ParcelRef | null>(null);
  const reportedEmpty = useRef<string | null>(null);
  const latestValue = useRef(value);
  useEffect(() => {
    latestValue.current = value;
  }, [value]);

  const qc = useQueryClient();
  const track = useTrack();
  const t = useT();
  const words = useMemo(() => searchWords(t), [t]);
  const { data: profile } = useMunicipality();
  const zones = useZones(touched);
  const { selectPoint, selectParcel, selectZone } = useSelection();
  const kos = useMemo(() => profile?.cadastral_municipalities ?? [], [profile]);
  const zoneList = zones.data?.zones;

  const empty = value.trim().length === 0;
  const typed = !empty && !(picked !== null && picked === value);
  const debounced = useDebounced(value, 250);

  // a parcel reference is not an address: the geocoder is not asked
  const looksLikeParcel = typed && parseParcelQuery(value, kos) !== null;
  const geocode = useGeocode(typed && !looksLikeParcel ? debounced : "");
  const hitsFresh = !!geocode.data && !geocode.isPlaceholderData && debounced.trim() === value.trim();
  const hits = hitsFresh ? geocode.data!.results : NO_HITS;

  const { parcel, items } = useMemo(
    () => (typed ? buildSuggestions({ query: value, kos, zones: zoneList, hits, words }) : { parcel: null, items: [] }),
    [typed, value, kos, zoneList, hits, words],
  );
  const showNotFound = !!parcel && sameRef(notFound, parcel);

  const options = useMemo<Option[]>(() => {
    if (empty) return recent.map((item) => ({ kind: "item", item, recent: true }));
    const list: Option[] = [];
    for (const item of items) {
      const isParcelRow = item.key.startsWith("parcel:");
      if (!(isParcelRow && showNotFound)) list.push({ kind: "item", item, recent: false });
      // after a miss every KO is offered again: "choose another" must not need retyping
      const chips = showNotFound ? kos : (parcel?.kos ?? []);
      if (isParcelRow && parcel && kos.length > 1) for (const ko of chips) list.push({ kind: "ko", ko });
    }
    return list;
  }, [empty, recent, items, parcel, kos, showNotFound]);

  const geoSettled = looksLikeParcel || value.trim().length < 2 || (hitsFresh && !geocode.isFetching);
  const zonesSettled = !!zones.data || zones.isError;
  const showEmpty =
    typed && value.trim().length >= 2 && geoSettled && zonesSettled && options.length === 0 && !showNotFound;
  const listOpen = open && (options.length > 0 || showEmpty || showNotFound);
  const current = active < options.length ? active : -1;
  const firstKo = options.findIndex((o) => o.kind === "ko");

  /** A query that ended with no result: one `search_performed { matched: false }` per query. */
  const reportNoResult = (query: string) => {
    const key = normalize(query);
    if (!key || reportedEmpty.current === key) return;
    reportedEmpty.current = key;
    track("search_performed", { search_kind: "address", matched: false });
  };

  const close = () => {
    if (showEmpty) reportNoResult(value);
    setOpen(false);
    setActive(-1);
  };

  const onOutsideClick = useEffectEvent((e: globalThis.MouseEvent) => {
    if (open && !wrapRef.current?.contains(e.target as Node)) close();
  });
  useEffect(() => {
    const listener = (e: globalThis.MouseEvent) => onOutsideClick(e);
    document.addEventListener("click", listener);
    return () => document.removeEventListener("click", listener);
  }, []);

  const remember = (item: SearchItem) => {
    if (item.action.type !== "choose-ko") setRecent(pushRecent({ ...item, action: item.action }));
  };

  /** The row's label goes into the input, the list closes, the search gives the map back. */
  const commit = (label: string) => {
    setValue(label);
    setPicked(label);
    setNotFound(null);
    setOpen(false);
    setActive(-1);
    inputRef.current?.blur();
  };

  const runParcel = async (target: ParcelRef, isRecent: boolean) => {
    setNotFound(null);
    setPending(target);
    const outcome = await selectParcel(target, { recent: isRecent });
    setPending((p) => (p === target ? null : p));
    if (outcome === "found") {
      const item: SearchItem = {
        key: `parcel:${target.ko}:${refLabel(target)}`,
        icon: "#",
        title: parcelTitle(target, words),
        sub: t("search.cadastralRef", { ko: target.ko }),
        action: { type: "parcel", ref: target },
      };
      remember(item);
      commit(`${item.title} · ${target.ko}`);
    } else if (outcome === "not_found") {
      // said inline; the search stays open on the reference so another KO is one click away
      if (isRecent) {
        setValue(`${parcelTitle(target, words)} · ${target.ko}`);
        setPicked(null);
      }
      setNotFound(target);
      setActive(-1);
      setOpen(true);
      inputRef.current?.focus();
    }
  };

  const activate = (option: Option) => {
    if (option.kind === "ko") {
      if (parcel) void runParcel({ ko: option.ko, number: parcel.number, sub: parcel.sub }, false);
      return;
    }
    const { item, recent: isRecent } = option;
    const action = item.action;
    switch (action.type) {
      case "choose-ko":
        if (firstKo >= 0) setActive(firstKo);
        return;
      case "parcel":
        void runParcel(action.ref, isRecent);
        return;
      case "address":
        remember(item);
        commit(item.title);
        void selectPoint(action.point, "address", { recent: isRecent });
        return;
      case "zone":
        remember(item);
        commit(item.title);
        selectZone(action.zone, { recent: isRecent });
        return;
    }
  };

  /** Enter with no row highlighted: the first row, once the suggestions for this very text are in. */
  const submit = async () => {
    if (!typed) {
      if (empty && options[0]) activate(options[0]);
      return;
    }
    if (parcel) {
      if (parcel.ko) void runParcel({ ko: parcel.ko, number: parcel.number, sub: parcel.sub }, false);
      else if (firstKo >= 0) setActive(firstKo);
      return;
    }
    const text = value;
    let first: Option | undefined = options[0];
    if (!hitsFresh && text.trim().length >= 2) {
      const q = text.trim();
      let fresh: readonly GeocodeResult[] = NO_HITS;
      try {
        const res = await qc.fetchQuery({
          queryKey: queryKeys.geocode(q),
          queryFn: ({ signal }) => api.geocode(q, { signal }),
          staleTime: 5 * 60_000,
        });
        fresh = res.results;
      } catch {
        // the geocoder never dead-ends; an outage is simply no rows
      }
      if (latestValue.current !== text) return; // the visitor kept typing
      const item = buildSuggestions({ query: text, kos, zones: zoneList, hits: fresh, words }).items[0];
      first = item ? { kind: "item", item, recent: false } : undefined;
    }
    if (first) activate(first);
    else {
      reportNoResult(text);
      setOpen(true);
    }
  };

  const move = (delta: number) => {
    if (options.length === 0) return;
    setOpen(true);
    setActive((i) => (i < 0 ? (delta > 0 ? 0 : options.length - 1) : (i + delta + options.length) % options.length));
  };

  const optionId = (i: number) => `${listId}-${i}`;
  const keepFocus = (e: MouseEvent) => e.preventDefault(); // clicks in the list keep the focus in the input

  const renderOption = (i: number): ReactNode => {
    const option = options[i];
    const props = {
      id: optionId(i),
      type: "button" as const,
      role: "option",
      "aria-selected": i === current,
      onMouseDown: keepFocus,
      onMouseEnter: () => setActive(i),
      onClick: () => activate(option),
    };
    if (option.kind === "ko") {
      return (
        <button
          key={`ko:${option.ko}`}
          {...props}
          className={option.ko === (showNotFound ? notFound?.ko : parcel?.ko) ? "kochip on" : "kochip"}
        >
          {option.ko}
        </button>
      );
    }
    const { item } = option;
    const busy = item.action.type === "parcel" && sameRef(pending, item.action.ref) && pending?.ko === item.action.ref.ko;
    const sub = busy ? `${item.sub} · ${t("search.lookingUp")}` : option.recent ? `${t("search.recent")} · ${item.sub}` : item.sub;
    return (
      <button key={`${option.recent ? "recent:" : ""}${item.key}`} {...props} aria-busy={busy || undefined}>
        <span className="ico">{item.icon}</span>
        <span>
          <div>{item.title}</div>
          <div className="sub">{sub}</div>
        </span>
      </button>
    );
  };

  const notFoundText = notFound ? t("search.notFound", { ref: refLabel(notFound), ko: notFound.ko }) : "";
  const koPending = pending && parcel && sameRef(pending, parcel) ? pending.ko : null;

  return (
    <div className={open ? "searchwrap searching" : "searchwrap"} ref={wrapRef}>
      <span className="mag">
        <IconSearch />
      </span>
      <input
        ref={inputRef}
        id="search"
        type="text"
        role="combobox"
        aria-expanded={listOpen}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={current >= 0 ? optionId(current) : undefined}
        aria-label={t("search.label")}
        placeholder={t("search.placeholder")}
        autoComplete="off"
        spellCheck={false}
        enterKeyHint="search"
        value={value}
        onChange={(e) => {
          setValue(e.target.value);
          setActive(-1);
          setNotFound(null);
          setOpen(true);
        }}
        onFocus={() => {
          setTouched(true);
          setRecent(readRecent());
          setOpen(true);
        }}
        onBlur={(e) => {
          // Tab to another control closes; a click elsewhere is the document listener's
          if (e.relatedTarget && !wrapRef.current?.contains(e.relatedTarget as Node)) close();
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            close();
            return;
          }
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            move(e.key === "ArrowDown" ? 1 : -1);
            return;
          }
          if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && current >= 0 && options[current].kind === "ko") {
            e.preventDefault();
            const chips = options.flatMap((o, i) => (o.kind === "ko" ? [i] : []));
            const at = chips.indexOf(current);
            setActive(chips[(at + (e.key === "ArrowRight" ? 1 : -1) + chips.length) % chips.length]);
            return;
          }
          if (e.key === "Enter") {
            e.preventDefault();
            setOpen(true);
            if (current >= 0) activate(options[current]);
            else void submit();
          }
        }}
      />
      <span className="kbd" aria-hidden>
        ⌘K
      </span>
      <button
        type="button"
        className="searchcancel"
        onMouseDown={keepFocus}
        onClick={() => {
          close();
          inputRef.current?.blur();
        }}
      >
        {t("search.cancel")}
      </button>
      <div className={listOpen ? "searchsug on" : "searchsug"} id={listId} role="listbox" aria-label={t("search.suggestions")}>
        {showNotFound && (
          <div className="searchnote" role="option" aria-disabled="true" aria-selected={false}>
            <span className="ico">#</span>
            <span>
              <div>{notFoundText}</div>
              <div className="sub">{t("search.notFoundHint")}</div>
            </span>
          </div>
        )}
        {groupRows(options).map((row) =>
          row.kind === "one" ? (
            renderOption(row.at)
          ) : (
            <div key="kopick" className="kopick" role="group" aria-label={t("search.koGroup")}>
              <span className="kolbl">{koPending ? t("search.koPending", { ko: koPending }) : t("search.koGroup")}</span>
              {row.at.map(renderOption)}
            </div>
          ),
        )}
        {showEmpty && (
          <div
            role="option"
            aria-disabled="true"
            aria-selected={false}
            style={{ padding: 14, fontSize: 12, color: "var(--ink-2)" }}
          >
            {t("search.noMatch")}
          </div>
        )}
        {empty && options.length === 0 && (
          <div className="searchhint">{t("search.hint")}</div>
        )}
      </div>
      <span className="sr-only" aria-live="polite">
        {showNotFound ? notFoundText : showEmpty ? t("search.noMatch") : ""}
      </span>
    </div>
  );
});
