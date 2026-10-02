# UrbanView frontend — working notes for Claude

The public map of UrbanView (Podgorica pilot). Next.js 16 (App Router) + TypeScript + Radix
primitives (shadcn/ui's), Mapbox GL JS, TanStack Query, zustand; styled by the wireframe stylesheet
(no Tailwind: it was imported but no utility class was used; removed 2026-10-01). Product rules,
API contracts and backend conventions are in the root `CLAUDE.md`; the full design contract is
`docs/specs/frontend-design.md`. This file is what the frontend tasks reference.

## The design rule

**The UI reproduces the client-approved interactive wireframe exactly** (structure, labels,
tokens, sizes, copy, states). The BRQ's "not strictly" does not apply: this build is the approved
design. Proof, not taste: `docs/wireframe/screens/*.png` are the acceptance references (the
states the POC builds) and `docs/wireframe/computed-styles.json` holds the effective style of 202
selectors.

- `src/styles/wireframe.css` is a **byte-for-byte copy** of `docs/wireframe/wireframe.css`
  (`npm run design:check` fails otherwise), which is the mock's stylesheet minus the rules of the
  components the POC does not build (AI assistant, subscription plans, locks and paid states,
  badges, card payment, admin KPI tiles, the phase strip, dependency notes, the 860 / 760 px
  tablet and phone layouts; removed 2026-10-01). Never edit one copy alone. Its late override passes ("luxe",
  "type consolidation", "font roles") set the effective sizes, and
  `[class][class][class]{border-radius:8px}` gives every classed element an 8 px radius: that is
  load-bearing, do not "clean it up".
- Every deviation lives in `src/styles/overrides.css`, one commented block per reason (fonts,
  a11y helpers, Mapbox).
- **No Tailwind, no reset**: the wireframe stylesheet ships its own reset; `app/layout.tsx`
  imports `wireframe.css` then `overrides.css`, nothing else.
- Components render the wireframe's own markup and class names (templates in
  `docs/wireframe/wireframe.js`): `.phead > .pclose + .peyebrow + .ptitle + .psub`,
  `.idgrid > .idcell > .k + .v`, `.prow > .pk + .pv > .u`, `.cta.gold` … Do not restyle a component
  with utilities when the stylesheet already styles it.
- Icons are the wireframe's inline SVGs (`src/components/ui/icons.tsx`), not an icon set.
- Where the ticket text and the rendered wireframe disagree, the rendered wireframe wins and the
  difference is written down: the modal radius is **8 px** (the 18 px declaration is overridden),
  the panel has **no shadow** (the `-8px 0 24px` declaration is overridden), status labels are a
  dot + word, not chips.

- **Copy is minimal** (product owner, 2026-10-02; overrides the mock's wording, not its
  structure): explanatory paragraphs are one short sentence or gone, and the UI makes no
  statement about AI or language models. Shortened: the empty panel's body, the disclaimer
  placeholder ("Figures are indicative ranges, not investment advice."), both intent toasts
  (each still says what was noted: "…in more market data" / "…in asking about this site"), the
  urban-parcel card and "Not defined" texts, the engine
  strip and modal, the methodology's lead, the order form's lead / context strip / guest note /
  pricing note ("Priced by parcel size: …"), the order failures and the order page's leads, and
  the admin cards' sub-lines. Removed: the zone panel's "In Montenegro a zone isn't…" paragraph.
  Where a section below quotes a longer sentence, the short one in the code is right.

Check a change visually against the reference: `npm run build && npm run start`, screenshot at
1440×900 (headless Chrome `--window-size=1440,900 --force-device-scale-factor=1`) and diff the
chrome regions against `docs/wireframe/screens/<state>.png` (topbar, rail, panel, legend,
coordinates and zoom tools were pixel-identical at setup).

## Tokens (`:root` of wireframe.css; the third column is the old Tailwind alias, for reference)

| Token | Value | Tailwind | Use |
|---|---|---|---|
| `--ink` | `#2A2118` | `ink` | text, dark surfaces (ROI hero, toast, coverage pill) |
| `--ink-2` | `#6B6152` | `ink-2` | secondary text, row keys |
| `--ink-3` | `#A79E8C` | `ink-3` | tertiary |
| `--paper` | `#F4EFE6` | `paper` | page, CTA stack, modal footer, inset cards |
| `--paper-2` | `#ECE4D6` | `paper-2` | list dividers, range track, icon tiles |
| `--line` | `#DDD3C3` | `line` | every hairline |
| `--white` | `#FBF8F2` | `white` | rail, panel, legend, modal, cards |
| `--brand` | `#B5613B` | `brand` | terracotta: primary action, selection, eyebrows, focus |
| `--brand-dark` | `#8F472A` | `brand-dark` | brand hover, planned-parcel outline, "on" layer name, computed GFA |
| `--brand-tint` | `rgba(181,97,59,.11)` | `brand-tint` | tinted chips, hovers |
| `--paid` | `#B08A2E` | `paid` | gold: the order CTA and the urban-parcel card (no subscription badge or locks in the POC) |
| `--paid-dark` | `#8C6B22` | `paid-dark` | |
| `--paid-tint` | `rgba(176,138,46,.13)` | `paid-tint` | gold tints |
| `--danger` | `#A24A32` | `danger` | |
| `--z-res` `--z-com` `--z-mix` `--z-pub` `--z-grn` | `#B5744A` `#BE9A44` `#8A7A8E` `#5E8A82` `#7C8A4F` | `z-res` … | Residential, Commercial, Mixed use, Public / institutional, Green / recreation |
| `--r` / `--r-lg` | `8px` / `12px` | `rounded-md` / `rounded-lg` | effective radius is 8 px on classed elements |
| `--shadow` | `0 1px 2px rgba(42,33,24,.05), 0 1px 3px rgba(42,33,24,.07)` | `shadow-sm` | map buttons, doc items, admin cards |
| `--shadow-lg` | `0 12px 32px -8px rgba(42,33,24,.20), 0 2px 6px rgba(42,33,24,.07)` | `shadow-lg` | legend, suggestions, modal, toast, pill |
| `--disp` / `--body` | Schibsted Grotesk 400–800 | `font-sans`, `font-display` | everything but data |
| `--mono` | JetBrains Mono 400 / 500 / 700 | `font-mono` | numbers, refs, coordinates, micro-labels (tabular-nums via `.mono`) |

Fonts come from Google Fonts through `next/font` (self-hosted at build, `latin` + `latin-ext` for
č ć đ š ž); `overrides.css` points `--disp` / `--body` / `--mono` at the generated families.
Other literal colours: topbar `#241B12`, map area `#EDE6D6`, muted outlines `#B3A894`,
gold-button hover `#6D28D9` (a purple in the delivered
file, reproduced; open item). Effective type scale: titles 26 / 700, section labels 20 / 700,
figures 44 / 700, body 14, secondary 13, mono data 13 / 700, uppercase mono micro-labels 11.5 with
.16em tracking.

## Dimensions

| Element | Geometry |
|---|---|
| Topbar | 62 px, `#241B12`, padding 0 20, gap 20; logo 36 px high (white via filter); search max 560 × 40, placeholder "Search an address, click the map, or enter a parcel number…", magnifier left, shortcut hint right (`⌘K` on macOS, `Ctrl K` elsewhere; none in the server render); nav "Map" (active: brand) / "Admin"; pill "PODGORICA · PILOT" (profile name, re-centres the map) |
| Layer rail | 206 px (`--white`, hairline right), collapses to 46 px with a 34 px opener; cards 182 × ≥ 32, 24 px swatch, 16 px check |
| Map | fills the middle, `#EDE6D6`; legend top-left 16/16 (max 264 wide); coverage pill top-centre; scale bar left 16 / bottom 52; coordinates chip left 16 / bottom 16; tools right 16 / bottom 88 (zoom label, 42 px `+` `−` reset) |
| Panel | 392 px (`--white`, hairline left, no shadow); sticky header, scrolling body, CTA stack; hidden (never replaced by anything) when the location is uncovered |
| AI | not built (pilot scope: "intent button only"): no fab, no chat panel, no quota; "Ask about this site" in the parcel panel's button stack records `ai_interest` |
| Admin | overlay `inset: 0` over the main row, `--paper`; admin bar 60 px; tabs in the pipeline's order: "Documents" (A1), "AI review queue" (A2), "Publish" (A4), "Financial assumptions" (A5), "Orders" (A6), "Analytics", "Audit log" (A7, thin tables; the wireframe's Overview, Planning rules and Calculation engine tabs are not in the POC plan; the bar scrolls sideways when narrow); "← Back to map" |
| Modal | overlay `rgba(20,17,14,.55)`, padding 24; modal max 520 (`wide` 860), max-height 90vh, radius 8 (effective), pop-in .2s |
| Toast | bottom-centre, ink, `✦` + text, 2.6 s |
| ≤ 1100 px | panel 360, rail 182 (wireframe) |

Desktop-first; no login wall, splash or onboarding tour. The intro toast ("Click any parcel to see
what can be built", 0.9 s after load) is part of the wireframe, not a tour.

## Map layers (registry `src/lib/layers.ts` → `LAYERS`, styles `src/lib/map/style.ts`)

One config array drives the rail, the map and the legend: each entry has its id, name, group,
default, rules (core / `choropleth`), swatch, source-layers in the PMTiles
archive, minimum zoom (the catalogue's; the tile pointer's per-layer `min_zoom` wins,
`layerMinZoom`) and a `legend(ctx)` function; `style.ts` → `LAYER_GROUPS` lists the map layers of
each entry. Rail order, groups and names are the wireframe's.

| Group | Layer | Default | Rule | Published tile layer(s) |
|---|---|---|---|---|
| Base | Urban zones | on | covered zones only (type fill, outline, label); a click opens the zone panel | `zones`, `zone_labels` |
| Base | Urban blocks | on | block boundaries (dotted) with block refs (z15+) | `urban_blocks` |
| Parcels | Cadastral parcels | on | never merged with planned parcels | `cadastral_parcels` |
| Parcels | Urban parcels | on | planned parcels, dashed brand outline | `urban_parcels` |
| Context | Land use | off | zone-type tint by `category` | `land_use` |
| Context | FAR heatmap | off | parameter choropleth; field chips FAR / Coverage / Floors / GFA | `heat_far`, `heat_coverage`, `heat_height`, `heat_gfa` |
| Feasibility | Price heatmap | off | sale-price choropleth, free like every card; field chips Low / Expected / High | `heat_sale_price` |

Two core layers are drawn but have no card (the POC plan's rail is exactly the seven toggleable
layers above; public-app check 2026-09-30): the base map (Mapbox style) and the planning-document
coverage areas (`document_coverage`, a click opens the document panel). They stay in `LAYERS` as
`core` entries so the style, the `?layers=` parameter and `layerState` keep working; the rail
(`CARDS`) and the legend skip them.

Not in the POC (scope audit 2026-09-28): the wireframe's Public ownership and Restitution / legal
cards (only with confirmed bulk cadastral access; the API marks those layers unavailable) and
Planned traffic (an MVP layer; not published).

- **Cards** are toggled by clicking (swatch + name), each independently (`toggleLayer`): the POC
  plan's seven layers (zones, blocks, cadastral and planned parcels, land use, the two heatmaps,
  which may be on together) each have their own card, unlike the mock's core zones card with the
  blocks folded in and its one-heatmap rule (S1 check, 2026-09-28). States: on / off. The rendered wireframe has no tint on an "on" card and an 8 px card radius (its
  late CSS passes override the 10 px and the tint); that is what ships.
- **A card never claims a layer the map is not drawing.** `layerState` (`lib/layers.ts`) is the one
  answer the rail, the legend and the map share: `off` / `no_data` (the published version lists
  its source-layers with 0 features: muted "no data yet" on the card, the legend group with that
  note and no rows, no style layer added) / `zoom_in` (on, but the map is below the zoom its data
  is built from: parcels from 13, heatmap and context layers from 10, plan areas from 9, zones and
  zone cells from 8; muted "zoom in to see" on the card under any field label, the legend group
  keeps its rows with the note by its title) / `shown`. Turning on a layer that would show
  nothing toasts why ("… shows when you zoom in closer", "…: nothing is published for this layer
  yet"). `useLayerStates` reads it from the store
  (`zoom`, set by the map with the zoom label) as one string, so the rail and legend re-render
  when a state changes, not on every camera frame. A rail click reads the store at the click,
  never the last render's snapshot.
- **Choropleths** (`src/lib/map/classes.ts`): one source-layer per heatmap (`heat_<layer>`, the
  field chips pick which of the four planning layers is drawn: the others' filters are `NONE`);
  legend rows come from the classes `/v1/tiles/current` serves (`cell_classes`, stored with the
  cells: quintile breaks for the planning layers, the profile's €/m² bands for the sale price, 0 =
  "not saleable") and the map colours each cell by the `band` the API gave it (`band_low` /
  `band_high` for the low / high price), so legend and map always match. Height is floors above
  ground (`max_floors`; the URL code stays `height`). Parameter classes run along #EFE3CE →
  #B4744A; price bands use the wireframe's gold steps. **Cells without a value get the "no data"
  hatch** (a separate layer on `!has("value")`), never the lowest class. Without served classes the wireframe's look is the
  fallback (a continuous gradient, "Low → high"; the fixed bands). Field changes restyle with
  `setPaintProperty` / `setFilter`; toggles with `setLayoutProperty`: no source reload.
- **Legend** (`legendGroups`): one group per layer that is on, in rail order, the wireframe's rows
  (zone types, "Urban block boundary", "Parcel outline", "Parcel — click to open", "Coverage area
  — click to open", FAR "Low →
  high" with unit "floor area ratio", price bands with unit "€/m² of floor area": the layer is the sale price per m² of floor area, not a land price), class rows for served
  classes and "No data" when some cells have none; a `note` by the title for a layer that is on
  but not drawn ("zoom in to see", "no data yet", see `layerState`); "No overlays active" when
  nothing is on.
- **`?layers=`** (`lib/url-state.ts`): the toggleable layers that are on with the choropleth field
  (`landuse,heatFAR:gfa`, `heatMkt:low`); absent for the default view, `none` for nothing on; a
  link restores it on load, then the parameter follows the rail.
- **Events**: `layer_toggled { layer_id: <published key>, on }` on every toggle.

## Map (S1 "Map — Landing": `src/components/shell/map-view.tsx`, `src/lib/map/*`)

- **Mapbox GL JS, lazy.** Imported in its own chunk only when `NEXT_PUBLIC_MAPBOX_TOKEN` is set
  (Mapbox wipes the canvas without a valid token; there is no token-less mode). Base style
  `NEXT_PUBLIC_MAPBOX_STYLE` (default `light-v11`). Opens on the profile's bounds, the city extent
  (S1), or a `?parcel=` link's parcel. 2D only: `projection: "mercator"` overrides the style's
  globe (whose max bounds only hold the centre, and not before the style loads), rotation / pitch
  off. 1.0× = the city framing of the map's box; zoom 0.4× of it … 19; the max bounds are the
  field the 0.4× view shows around the city's centre, so zooming all the way out ends on the whole
  city, centred (the wireframe's rule), and panning closer in stops at that field. Framing, zoom
  range and field are measured again on every resize of the box (rail, panel, window);
  reset returns to 1.0×. Fit options never carry `maxZoom: undefined` (`lib/map/camera.ts`:
  Mapbox spreads it over its default, the fitted zoom turns NaN and the fit is dropped, which once
  opened the map on a corner of its bounds over Skadar Lake), and the padding shrinks to the box
  (`fitPadding`). `clickTolerance: 3` = a drag of more than 3 px never selects.
- **The map follows the rail exactly.** Once the style has loaded (`style.load`, which also adds
  UrbanView's source then, so its tiles download alongside the base map's instead of after them),
  every change of the layers, the choropleth fields and the selection is applied the moment
  it happens, also while tiles are still loading (layout, filter and paint changes do not need
  them; gating on `isStyleLoaded()`, false whenever a source is loading, dropped changes and left
  the map out of step until the next toggle). Before `style.load` its handler applies the state as
  it is then; a refused change re-applies the whole state at the next `idle`.
- **PMTiles on Mapbox.** Mapbox GL has no `addProtocol` (MapLibre's API); 3.x has the experimental
  `mapboxgl.addTileProvider(name, moduleUrl)`: every map worker imports the module and asks it for
  TileJSON and tiles. `src/lib/map/pmtiles-provider.ts` is that module (pmtiles library,
  worker-safe, no app imports), bundled by `scripts/build-map-provider.mjs` into
  `public/map/urbanview-pmtiles.js` (runs as `predev` / `prebuild`; generated, git-ignored). The
  source `url` is `/v1/tiles/current#archive=…&expires=…&version=…` (`lib/map/tiles.ts`): workers
  start on the pointer the page already has and re-read the pointer shortly before the signed link
  expires or when a range request fails. A new `version_id` from `useTilesCurrent` recreates the
  source, so a publish swaps tiles without a deploy; the page reads the pointer again at least
  every 5 minutes and when the tab regains focus (`pointerRefreshMs`), so a publish
  reaches maps already open within 5 minutes (a reload at once). No archive (unpublished) = base
  map only, nothing drawn. The page reads `/v1/tiles/current` server-side together with the
  profile, so the
  first paint has it. The object store must allow CORS `GET` with `Range` from the site origin.
- **Layers** (`lib/map/style.ts`, wireframe values; fills and lines under the base map's labels,
  our labels on top): zones filled by `zone_type` at 24 % + boundary at 40 %, a label from the
  `zone_labels` point layer, for covered zones only: outside coverage the map shows the base map
  alone, no zone geometry (POC plan; `covered: false` zones are neither filled, outlined nor
  labelled); a selected zone gets a brand outline; dashed coverage; cadastral parcels tinted by their zone type,
  covered ones only (`covered: false` parcels are neither drawn, hovered nor outlined when
  selected; the archive carries no heatmap cell or block outside coverage either: S6 check
  2026-09-29); dashed
  planned parcels; block boundaries and refs (z15+); context overlays; hover layers (cursor
  pointer + outline) and selection layers (brand outline + glow; a cadastral selection also
  highlights its primary planned parcel). Visibility follows the rail (`visibleLayerIds`). Each
  layer starts at the zoom its source-layer is built from (`uvLayers(tiles)`:
  the pointer's range, else the catalogue's); a source-layer the pointer lists with 0 features
  gets no style layer. Text uses the base style's glyphs (`DIN Pro`), not the web fonts.
- **Base map** (`lib/map/basemap.ts`): with Mapbox's light style (the default) the style's own
  layers take the wireframe's colours on `load` (land `#EEF1F5`, white major and cream `#F2EBDC`
  minor roads, river `#BCCEC8` at .85 with a `#A4B9B2` line) and buildings, land cover, points
  of interest, airports and footpaths are hidden; place, street and water labels stay. A custom
  `NEXT_PUBLIC_MAPBOX_STYLE` is left as designed.
- **Click** (`lib/map/pick.ts`): features under the pointer on the hit layers; priority
  cadastral parcel > planned parcel > coverage area > zone. A zone (covered, so drawn, with no
  parcel or plan area under the click) becomes the selection with its outline and the zone
  panel, no pin. Cadastral: highlight + pin at the rendered
  centroid at once, then `/v1/locate` at the click confirms zone, planned link and coverage (the
  pin moves to the API centroid; uncovered = S6). Planned parcel: always covered (tiles hold
  adopted plans only). Coverage area: highlight, no pin. Nothing hit: pin + `/v1/locate`; the
  cadastral parcel there becomes the selection, else the planned parcel containing the point
  (`pointSelection`: plans served before the cadastral base still answer a click or an address
  with their urban parcel panel; the pin stays on the point), uncovered = S6, covered without a parcel = the empty
  panel's pin note.
- **S6** (`flashUncovered`): "Outside current coverage" pill (sub-text `no adopted plan`, or
  `outside <municipality>` for points outside the profile bounds) for 2.6 s with the panel closed,
  then the panel returns with the empty state and the pin's note "Pin dropped at 42.4180° N ·
  19.2851° E — no adopted plan published here yet." (`— outside Podgorica.` beyond the profile
  bounds; `empty.noPlan` / `empty.outside`, bilingual), so the pin never stands unexplained. Never
  an error, never a red state.
- **`?parcel=<Parcel ID>`** (`lib/url-state.ts`, `app-shell.tsx`): follows the selected cadastral
  parcel (`history.replaceState`); opening a link reads `/v1/parcels/{id}/panel` (centroid +
  bbox), selects and centres the parcel (the camera focus waits for the map). An unknown id drops
  the parameter quietly; an API outage keeps it.
- **Selection store** (`lib/store.ts`): `selection` = `point` | `parcel` (search pending) |
  `feature {type: cadastral | urban | document, id, zoneId, linkedUrbanId, via}` | `zone {id,
  name, via: search | click}` (a zone picked in the search is framed; either way it is outlined);
  `highlightOf`
  derives the map highlight; the panel reads the same selection.
- **Events**: `map_loaded` once the style and first tiles are in (`idle`, with `load_ms`; without a
  token on first render with `renderer: none`); `search_performed {search_kind, matched, lat, lng,
  …ids, coverage}` per search or map click (see "Search");
  `parcel_selected {parcel_id | urban_parcel_id, parcel_type, zone_id, via: click | search | link}`.

## Search (S2 "Find a Location": `src/components/shell/search-box.tsx`, `src/lib/search.ts`)

One input (the wireframe's; no mode tabs) takes an address, a zone name or a parcel number. The
rules are pure functions in `lib/search.ts` (unit-tested); the box renders them in the
wireframe's `.searchsug` rows: icon tile (⌂ address, # parcel reference, ▤ zone, ⚠ outside
coverage), title, mono sub-label.

- **Urban parcel number** (2026-10-02): text that starts with the profile's parcel abbreviation
  and holds one compact number (`UP 40`, `up40`, `UP C2962`; `parseUrbanQuery`) asks
  `GET /v1/locate/urban-parcel` (debounced, never the geocoder) and lists the planned parcels
  found, `UP 40` with sub `Urban parcel · <planning document>`. A pick selects that planned
  parcel by id (`selectUrbanParcel`: pin inside it, fly, the urban panel; `search_performed
  {search_kind: parcel_number, result: parcel, urban_parcel_id}`); Enter before the answer waits
  for it. A bare number stays a cadastral reference.
- **Rows, in order:** parcel reference, urban parcels, zones (≤ 3), addresses (geocoder, ≤ 8). Duplicates
  collapse. Nothing → `No match. Try a street name, a zone or a parcel number (e.g. 1042/3).` (the
  mock's row with its placeholder copy replaced, never an error). A picked row's label stays in the input and is not searched again.
- **Addresses:** `GET /v1/geocode?q=` debounced 250 ms, from 2 characters, never for a parcel
  reference; replies for older text are dropped (`isPlaceholderData`). Each hit is placed in its
  zone with the outlines of `GET /v1/zones` (fetched on first focus, cached 10 min): sub-label
  `Address · Centar` (`Street · …`, `Place · …`), or ⚠ `Outside coverage · no adopted plan` when
  the point is in no zone or in a zone without an adopted plan. The hint is advisory:
  `/v1/locate` decides after the pick (a covered zone can still hold an uncovered point).
- **Zones:** matched locally by word prefix, diacritics ignored (`normalize`); ▤ `Zone`, or ⚠
  `Zone · outside coverage · no adopted plan`. Pick = frame the zone's bbox and select it
  (`{kind: "zone"}`, the S3 zone panel); a zone without an adopted plan shows the S6 pill first,
  then the panel's "No adopted plan" state.
- **Parcel reference:** `1042`, `1042/3`, `#1042`, `parcel 1042/3`, `parcela 1042/3`, optionally followed by KO text
  (`1042/3 Podgorica II`, `1042, pod 2`, digits read as Roman numerals). Text after the number
  that names no KO makes it an address query. Row `Parcel #1042/3` with sub `Cadastral ref ·
  <KO>` when the KO is settled (a unique match, or a single-KO municipality), else `Cadastral ref ·
  choose the cadastral municipality` and the **KO picker** under it: chips of the profile's
  `cadastral_municipalities` narrowed by the typed KO text (Enter on the row moves to the first
  chip). `/v1/locate/parcel` runs only with a KO. The Parcel ID is never a search input.
- **Parcel not found:** inline row `No parcel 1042/3 in Podgorica III` + `Check the number or
  choose another cadastral municipality`, the list stays open with every KO offered again, the map
  keeps its previous selection. No toast, no error styling.
- **Outcomes:** address → pin, fly (z17), `/v1/locate` → parcel selected + panel (the cadastral
  parcel, else the planned parcel at the point), pin note when covered without either, S6 when uncovered; parcel → fly, select, panel or S6; zone as above.
  The search closes and blurs on a pick.
- **Recent searches:** the last 5 picks (address, zone, parcel with KO) in `localStorage`
  `uv.search.recent`, this browser only, shown when the input is focused and empty (sub-label
  `Recent · …`); a re-run emits `recent: true`. Validated on read (bad data is dropped).
- **Keyboard:** ⌘K / Ctrl+K focuses (shell), ↑ ↓ through rows and chips, ← → between chips,
  Enter picks the highlighted row, or the first row (waiting for the geocoder when its answer is
  not in yet), Esc closes. Combobox ARIA (`aria-activedescendant`, `role="option"` rows and chips,
  a polite live region for "No match" / "No parcel").
- **Events:** `search_performed { search_kind: address | parcel_number | click, matched }` +
  `result: address | zone | parcel` for typed searches (zones count as `address`, the enum has no
  zone kind) + `recent: true`. Address / zone picks are `matched: true`; a query left with no
  result (Enter, or closing the list on "No match") is one `{search_kind: address, matched:
  false}` per query text; parcel lookups report found / not found; a map click is matched when a
  parcel (cadastral or planned) is under it. Every event also says where it landed: `lat` /
  `lng` rounded to 4 decimals (≈ 11 m; the point clicked or picked, a parcel lookup's centroid)
  and the ids found (`parcel_id`, `urban_parcel_id`, `zone_id`, `document_id` for a plan-area
  click; a zone pick carries `zone_id` only) and `coverage`: `covered` (a parcel or plan feature
  inside coverage; zones, plan areas and planned parcels are drawn for covered plans only, and a
  cadastral click reports after `/v1/locate`), `no_parcel` (covered land, no parcel), `uncovered`
  (no adopted plan: the S6 "outside current coverage" hit) or `failed` (the lookup failed);
  omitted for a parcel number that matched nothing (`lib/selection.ts` `pointCoverage`). Never
  the query text. The second ticket's `search_address`,
  `search_parcel` and `search_no_result` are not in the API's enum: they map onto
  `search_performed` (open item in `docs/specs/frontend-design.md` §10).
- **Latency:** the public Photon instance answers in 1–3 s (production waits up to 3 s,
  `GEOCODER_TIMEOUT_MS=3000`; at the 1.5 s default most answers were dropped and every address
  said "No match"); the < 2 s query-to-panel target needs a self-hosted geocoder
  (`GEOCODER_BASE_URL`) or the Redis cache. Parcel and zone searches do not touch the geocoder.

## Information panel (S3): zone, planning document, cadastral and urban parcel (`src/components/panel/*`)

The panel follows the selection (`info-panel.tsx` → `PanelContent`): a zone (search) → `ZonePanel`,
a planning-document coverage area (map click, or a document in a zone's list) →
`DocumentPanel`, a cadastral parcel → `CadastralPanel`, a planned (urban) parcel → `UrbanPanel`,
nothing / a pending parcel search / a point without a parcel → the empty state ("Pick a parcel to
begin", hint chips, pin note). Each variant renders the wireframe's markup (`.pscroll > .phead`
sticky + `.sect`s, `.ctastack`), data from `GET /v1/panel?type=zone|document|cadastral|urban&id=`
(`usePanel`), and emits `panel_viewed { panel_type, …ids }` once its data is in (again on every
new open). ✕ clears the selection
and its map highlight. Loading: the header with what the selection knows and "Loading…"; API
trouble: a neutral note and "Try again"; a 404 (entity gone) returns to the map quietly.

- **Zone** (`renderPanelZone`): eyebrow `ZONE` + the zone type name (`ZONE_TYPES`), title, sub
  `Internal city division · ≈ city quarter`; "Planning documents", the mock's note ("This
  zone groups N."), one `.docitem` per current document version: name, meta `source PDF ·
  eRegistri · adopted 12 May 2019 · 4 parcels with data` (`documentMeta`: PDF when stored, the
  adoption date when known, parcels for a document the map covers, `not yet digitised` for an
  adopted one it does not; the registry name links to its eRegistri entry), status chip Adopted /
  In progress / Superseded. A covered document opens its document panel (its name is the button,
  the row answers a click too); the file icon of a stored document opens its page 1 in the source
  viewer. **No adopted plan:** a zone without an adopted document
  shows a "No adopted plan" note above whatever is listed, never an empty list (searching such a
  zone first shows the S6 pill, then this panel). "General planning information": the zone's
  summary (BRD §2.3), or a neutral note while none is written (no zone-level typical parameters:
  not in the POC plan). Closing note as in
  the mock, then the data version line (`DataVersionLine`: `Data version 9 · published
  <date>`, mono, also under the document, cadastral and urban panels and in the assumption
  sandbox, the order form and the order page: `useVersionName` shows the published version's
  number, which the tile pointer carries for the current label, and the label itself for an
  earlier version).
- **Planning document** (`renderPanelDoc`): eyebrow `PLANNING DOCUMENT` + `adopted plan` / `plan
  in progress` / `superseded plan`, title, sub `DUP — Detailed urban plan` (profile
  `terminology.document_types_en`, else `document_types`); "Document details" + source
  chip (the PDF's page 1 through the source viewer when stored, else the registry entry): name,
  type, status, `Adopted` date when known, source (`PDF · eRegistri`, the registry name a link to
  its entry), amendments in progress;
  "General planning information" = the zone's summary (else a neutral note that none is written);
  "Coverage": zones spanned, cadastral and urban parcel counts; CTA stack: line "How we read
  a planning document" (the methodology modal, step 2); no intent button (the pilot scope's two
  are on the parcel panels).
- **Methodology** (`shell/methodology-modal.tsx`, the wireframe's wide `.method` modal): six steps
  with the mock's diagrams and its copy with the English tidied (2026-10-02: same steps and
  meaning, nothing added), Back / Next step; the last step's gold "Order this analysis
  →" calls `onOrder` (the order form for the parcel on screen) or says "Pick a parcel on the map to
  order its analysis." (provisional copy).
- **Source viewer** (`components/source/source-viewer.tsx`, opened by `useOpenSource` in
  `lib/source.tsx`): every source chip and row source icon, and every stored document in a zone's
  list, opens the cited page in the app, in the wireframe's wide modal (`.modal.wide.srcmodal`,
  90vh). Map and panel stay mounted behind the overlay, so closing (✕,
  Esc, the overlay) returns to the same panel with the visitor's edited assumptions. Header:
  eyebrow "Source document", document name, `p.13` + status chip + "Max floor area ratio: 3.2" +
  the value's note. One signed link (`/v1/source/value/{value_id}` or
  `/v1/source/{document_id}/page/{page}`), then PDF.js renders the page: `pdfjs-dist` legacy
  build (older mobile Safari) imported lazily on the first open, never on the map's first load;
  worker copied to `public/pdfjs/` by `scripts/copy-pdf-worker.mjs` (`predev` / `prebuild`,
  git-ignored); range requests with auto-fetch and streaming off (64 KB chunks), so only the
  requested page's bytes are read. The page fits the width (× device pixel ratio); the value's
  `bbox` (PDF points, origin bottom-left) is a translucent brand rectangle scrolled into view. The
  rectangle is placed before the page is drawn (its place needs only the page's size), so the
  value is framed at once even while a heavy page is still drawing (2026-10-02).
  Controls: ‹ page input "of N" ›, − zoom %
  + (50–400 %), ← → keys, "Open PDF ↗" (the whole document at `#page=N` in a new tab; a fresh
  link when the current one expires within a minute: a value's by its value id, so the file it
  cites); a typed page beyond the last snaps back
  with "This document has 24 pages." beside the input. An expired link (403) is re-fetched once,
  silently. Loading: a page-shaped shimmer. Failure says why, never in red (`sourceFailure` in
  `lib/source-text.ts`, from the 404's details): "The PDF of this document is not stored in
  UrbanView yet." + "See the document in eRegistri ↗", "Page 30 is not in this document: it has
  24 pages.", "This value is no longer published…", or "This page could not be loaded. Check
  your connection and try again." + Retry (only connection / server trouble retries); the page
  controls are disabled while there is no page. The opener passes a `hint` (document name,
  page, field label, note, registry link), so the header names the document and page before the
  API answers and when it fails. `source_reference_opened { document_id, page, value_id? }` once
  per open. The
  object store must allow CORS `GET` with `Range` and expose `Accept-Ranges`, `Content-Range`,
  `Content-Length` (as for the tiles). PDF.js loading, the document cache and the box maths are
  shared with the admin review queue's `PdfPageView` (`lib/pdf.ts`).
- Text-valued rows (`PanelRow text`) may wrap: the mock's `.pv` is `nowrap` for its short strings;
  real land-use texts are longer. A value that fits looks exactly as in the mock.
- **Cadastral parcel** (`cadastral-panel.tsx`, `renderPanelCadastral`, `GET /v1/panel?type=cadastral`):
  eyebrow `CADASTRAL PARCEL` + zone type (the zone index, `useZoneTypeName`), title `Parcel
  #1042[/sub]`, sub `<zone> · <KO>`; IdGrid parcel number, KO, urban block, cadastral area,
  governing document (full width, 11 px); "Corresponding urban parcel": one gold `.upcard` per
  planned parcel over it (the first with the mock's text, every card with how much of the parcel
  it covers, m² and %), a click opens that urban parcel's panel (`selectLinkedParcel`,
  `parcel_selected {via: panel}`), then the comparison card (`Cadastral 1,370.9 m² → urban 959.6
  m². −30% taken for roads / public space.`; one planned parcel is compared by its basis
  area, `basis_area_m2`: the area its plan states, as the urban panel; a split lists every
  planned area and takes the delta on their total; a planned parcel as large or larger says so, never a silent zero) ending
  with the area the calculations use (`BasisLine`: the urban parcel's, for a split the one
  covering the largest share), then the data version line. No planned parcel: "Not defined" card
  (mock text). No adopted plan: a "No adopted plan" card with the API's coverage note (the S6
  pill shows first).
- **Urban parcel** (`urban-panel.tsx`, `renderPanelUrban` without the market section, which is
  its own item): eyebrow `URBAN PARCEL` + zone type, title `UP 12`, sub `<zone> · <KO>`, backlink
  `← cadastral parcel #1042`; IdGrid urban parcel, cadastral parcel(s), KO, urban block,
  governing document; `Parcel ID 1001` (mono meta line, `.parcelid`: UrbanView's id of the
  cadastral parcel, only when there is one; the planned parcel's database id is never shown);
  "Cadastral vs urban parcel": the comparison card + "All calculations use the urban parcel
  area." (or the cadastral basis with the API's reason; `no_cadastral_parcel` stated as such);
  the card shows one urban area, the basis (`basis_area_m2`: the plan's stated area, else the
  drawn parcel's), also in the cadastral → urban comparison, and when the two differ by 2 % or
  more (`stated_vs_geometry_delta_pct`) it adds "The shape drawn on the map measures Y m²." +
  "All calculations use the area the plan states." (not in the mock: the mismatch rule,
  2026-10-02); without a cadastral parcel: "No cadastral parcel is recorded under this urban
  parcel. Area in the plan: X m²." ;
  "Planning parameters" (source chip = the first cited page): the mock's seven rows —
  Land use designation, Max building height (`27.5 m · P+8`: metres and floors, each with its
  source; a parcel of several buildings states the floors per building, `(a) Po+P+3, (b) Pv,
  (c) P+1`: a height text longer than 24 characters wraps like a text value), Max site coverage
  (IZ) %, Floor Area Ratio (II), Planned parcel area (the plan's stated value, else the
  geometry's area with a tooltip), Max Gross Floor Area (brand-dark),
  Max coverage area (the engine's output): the plan's Group 1 (BRD: Parcel ID, planning
  document, land use, urban block, urban parcel number and area, max height / floors, IZ, FAR,
  max GFA; the identification ones in the IdGrid), an explicit list, nothing else. Building line,
  setback, parking, green area and utilities are extracted and reviewed but not shown (sweep
  2026-10-01). The data version line follows the section. Every stated value ends with a source icon (`.rowsrc`,
  `RowSource`): the cited page through the source viewer (`/v1/source/value/{value_id}`),
  `source_reference_opened`; its title and accessible name are the reference itself
  (`sourceRefText`: `Max number of floors: DUP Centar – Zona C2, page 14 · table 3 – UP 12`, +
  "plan-wide value" for a document-level fallback), so the height row's two icons (metres,
  floors) are told apart. Missing values read "Not stated" in words (`.notstated`; the reason
  as tooltip), never a dash; a land-use code is followed by its legend name when the API gives
  `value_name` (`SS · stanovanje srednje gustine`); computed rows carry their formula as
  tooltip. The section's source chip opens the first cited value by its value id, so the page
  opens with that cell framed and named, like a row's icon.
- **Parcel button stack, compact** (2026-10-02; `.ctastack.compact` in `overrides.css`): the
  gold order button at 44 px, the two intent buttons side by side at 36 px, the methodology as
  a text link: about 140 px instead of the mock's four 50 px buttons (270 px, a quarter to half
  of the panel on a laptop). Each intent button has its own acknowledgement in the string table
  (`market.noted`, `ai.noted`).
- **Parcel CTA stack** (`ParcelCtas`): gold "Order expert analysis" + price (`GET
  /v1/orders/pricing` tiers applied to the panel's `basis_area_m2` by `lib/pricing.ts`, the
  server's rule, so the price shown is the price charged; the click opens the S4 order form,
  see "Orders"), the pilot scope's two intent buttons, which log interest and say so in a toast,
  never opening, unlocking or changing anything (intent check 2026-09-28): ghost "Unlock full
  market data" (no padlock icon) on the urban parcel panel, where Group 2 shows
  (`market_data_interest {…ids, trigger: parcel_panel, panel_type}`, "Thanks — we have noted your
  interest in more market data.") and ghost "Ask about this site" on both parcel panels
  (`ai_interest {…ids, trigger: parcel_panel, panel_type}`, the string table's `ai.noted`:
  nothing is promised); then line "How we analyze
  this parcel" (methodology, step 1, context `Parcel #1042 · Podgorica I`). The POC has no AI
  assistant: no fab, chat panel, quota or chips.
- `panel_viewed`: cadastral `{panel_type, parcel_id, zone_id}`, urban `{panel_type,
  urban_parcel_id, parcel_id, zone_id}`.
- **Group 2, "Market data & feasibility"** (`market-section.tsx`, under the urban panel's
  planning parameters; wireframe `marketHTML`), shown to everyone: the POC has no subscription
  or paywall, so the wireframe's locked state (`LOCKED` chips, "Unlock →", the paid badge) is not
  built (scope audit 2026-09-28). ROI hero (ink, radius 16: expected %, "range low% — high% ·
  expected n%"; no decorative trend line), range rows (land value, construction, market value, profit: expected
  value, the 6 px `.rangebar` with the marker at the expected position, low / "expected" / high),
  design & documentation as a plain row with its range underneath (no single money figure),
  saleable area (deterministic), the assumption sandbox (below), the engine strip, the
  disclaimer (the wireframe's text until the API's `disclaimer_status` is `client_approved`,
  then the API's); its intent, "Unlock full market data", sits in the panel's button stack.
  Figures are the payload's `feasibility` block
  (the shared engine's output), or the engine's recalculation for the visitor's edits; a figure
  the engine cannot calculate reads "cannot calculate — <reason>" (e.g. no market data for the
  zone) and the others still show; never a made-up range. `financials_viewed {urban_parcel_id,
  parcel_id, zone_id}` once the section is on screen (IntersectionObserver, once per open; slider
  moves never re-send it).
- **Assumption sandbox** ("◐ Test your own assumptions", `assumption-sandbox.tsx`,
  `lib/assumptions.ts`): three range inputs as in the wireframe — construction €/m² (500–1200),
  sale price €/m² (1200–3600), saleable % (55–85): bounds are configuration (`SLIDERS`), widened
  to include a zone's default; starting values are the payload's `assumptions` (the zone's market
  row, 70 % share), never hard-coded. Each value is marked "default", or "your assumption ·
  default €860" once edited; the other rates in use (land, design), the market source and date,
  the market version with the date it applies from (`market_version`), the formula and data
  version sit under them; "Reset to defaults" clears the edits. Every change calls the shared engine package's
  `recalculate(engine.inputs, edits)` in the browser (`recalculateFeasibility`: maps panel keys
  onto the engine's with the payload's `engine.edit_keys` / `field_keys`, no formula of its own)
  and re-renders the ROI hero, range rows and saleable area at once (the markers glide); no
  edits = the payload's figures exactly. An edit outside its bounds (the sliders' and the
  server's: prices in (0, 100 000], share in (0, 1]) shows an inline error and is not
  calculated. Editing sends no request (the two engines are held equal by the shared fixtures
  and the cross-engine test). Edits live in the store (`assumptionEdits`, only the edited keys),
  in memory for the page: moving to another parcel keeps them (the other keys take that zone's
  defaults) and an order carries them; a reload returns to the defaults, and nothing is saved
  on the server. An edit that returns to the default value stops being an edit. Edits send no
  analytics event (the plan's 13 events have none; `assumption_edited` was removed 2026-10-01).
  No market data for the zone: the sliders are disabled.
- **"How the figures are calculated"** (`shell/engine-modal.tsx`, wireframe `openEngine`, from
  the engine strip): the poc-1 formulas table (name, expression, source), input
  data (planning documents, cadastre, the zone's market source or "no market data for this zone
  yet", the visitor's assumptions), "Indicative ranges, not investment advice. Deterministic calculation — no
  language model generates or edits these figures.", footer with engine and formula
  versions.
- **Orders: S4 "Order expert analysis" and S5 "Order confirmed"** (`components/order/*`,
  `lib/order.ts`, `lib/order-form.ts`), modals over the map as in `screens/order.png`,
  `order-legal.png`, `success.png`. No account, no password, no verification, no card.
  - *Where they start:* every order button calls `requestOrder(trigger)` (gold "Order expert
    analysis" = `panel`, the methodology's "Order this analysis →" = `methodology`). The parcel panel on screen registers its parcel as
    `orderTarget` (`ParcelCtas`: parcel type + id, `Parcel #1042/3` (the cadastral parcel, on the
    urban panel too, as in the mock), KO, the planned urban parcel (`UP 12`), the panel's
    `data_version`, `basis_area_m2`, `calculation_basis`, event ids), so
    the location is carried through, never re-entered; without a parcel panel the button toasts
    "Pick a parcel on the map to order its analysis." `order_started {…ids, panel_type, product:
    expert_report, trigger}` on every start.
  - *S4:* the mock's markup: gold-tinted header, context strip `Analysing Parcel #1042 ·
    Podgorica I (urban parcel UP 12) — carried through automatically…` with `Planning data
    version <label>` on its own line (`.ctxver`), order summary (parcel size with `urban
    parcel` / `cadastral parcel`, analysis fee with its band `up to 500 m²` / `over 500 m²`,
    expected delivery `5 working days`: `GET /v1/orders/pricing` through `lib/pricing.ts`, the
    server's rule), the pricing note written from the tiers, "Ordering as" Individual | Legal
    entity (the pilot scope's guest form, not the mock's: first / last name, telephone, email;
    "Legal entity" adds company name and `PIB` above them, both `optional`; switching keeps what
    was typed), the methodology card (opens the wizard; its last step returns to the form), the
    guest note ("order as a guest"); footer `€200 · 5 working days`, Cancel, gold "Place
    order →" (the mock's "Continue to payment →": there is no payment step).
  - *Validation* (`validateDraft`, the API's rules): first name, telephone and email for both
    types (last name, company name and PIB optional); the server's email and telephone patterns
    and lengths. Inline messages under the fields, focus on the first. One request at a time
    (disabled button + guard).
  - *Failure:* the form stays with everything typed and one sentence under it
    (`explainFailure`: connection / server trouble, the per-email daily cap, the rate limiter, a
    parcel that is gone, field errors mapped back onto the fields). What was typed lives in the
    store (`orderDraft`, memory only, never storage) and is cleared once an order is placed.
  - *Language:* the request carries the language the map is in (`language`, `useLang()`):
    the order's e-mails are written in that one language, now and when the report is delivered.
  - *Request:* `POST /v1/orders` with the location, the name, telephone and email, a legal
    entity's company name and PIB when given (else null), and the visitor's edited assumptions
    (the server snapshots the panel with them). 30 s timeout.
  - *S5:* the mock's `.success` block (check, "Order confirmed", "…email it within **5 working
    days**. A confirmation is on its way now.", the mono chip `UV-PODI-UP-12-260924-01 · Parcel
    #1042`), then "Pay by bank transfer": the API's instructions as `.paysummary` lines (payee,
    IBAN, bank and SWIFT when configured, payment reference, "Amount due" as the total line;
    "Copy" on IBAN and reference), the API's note (work starts when the payment is received),
    "The same instructions were emailed to <address>" and "Track your order ↗" (the order page).
    An email the API did not queue or send (`email_status` suppressed | failed: no SMTP on the
    server) is said instead ("The confirmation email could not be sent, so please keep the
    details below.", toast "Order placed — keep your order reference"). `checkout_completed {…ids, panel_type,
    product, order_id: <reference>, amount_eur, currency}` when the order is created; "Done"
    closes with the toast "Order placed — check your email". Reload-safe: while it is open the
    URL carries `?order=<reference>` (`lib/url-state.ts`); the shell reopens it on load from
    `GET /v1/orders/{reference}` (`reopenConfirmation`: the order's status as it is now,
    "Status: awaiting payment.", never the first moment's "a confirmation is on its way" and no
    claim about an e-mail; "Your payment has been received" and no instructions once paid; an
    unknown reference drops the parameter). Closing it removes the parameter.
- **Order page** (`app/orders/[reference]`, `components/order/order-status.tsx`): the link of the
  confirmation and of every order email (`ORDER_PUBLIC_BASE_URL/orders/<reference>`). `GET
  /v1/orders/{reference}` (`useOrder`: status, location, price, turnaround, data version, the
  payment instructions while the payment is due; no personal data, no login), read again on tab
  focus and with "Check again". No mock screen: the topbar with the logo and a card in the
  modal's style: status as the title (`Awaiting payment`, `Payment not received`, `Paid`, …) with a
  line on what happens next, the reference chip, the steps (order placed → payment received →
  expert at work → report delivered, dot + word labels, dates of the first and latest step; a
  failed payment still waits for step 2), "Pay by bank transfer" (the confirmation's
  `PayInstructions`) while the payment is due, the order rows (location, analysis fee, placed,
  last update, planning data version, expected delivery), "← Back to the map". Unknown
  reference: "Order not found". `noindex`.

## Panel fields

**Identification** (IdGrid): parcel number, cadastral municipality (KO, mandatory: numbers repeat
across KOs), urban block, cadastral area, governing document (full width); urban panel adds urban
parcel and cadastral parcel. Parcel ID (`cadastral_parcels.id`) is UrbanView's own id, never
composed from these.

**Cadastral vs planned** are separate objects, never merged: the urban panel shows "Cadastral vs
urban parcel" (`Cadastral a m² → urban b m². −d% taken for roads / public space.` + "All
calculations use the urban parcel area."), the cadastral panel the gold "Corresponding urban
parcel" card or "Not defined". Any area mismatch is always shown (`area_comparison`).

**Group 1: planning parameters**, `source` chip. The payload's `planning.fields` carries all 13 fields of the
dictionary in this order (the panel shows the plan's Group 1 of them, see "Urban parcel"), every stated value with its source (document +
page, one click to the cited page via `viewer_url`); `not_stated` renders "Not stated":

| Key | Label | Unit |
|---|---|---|
| `land_use` | Land use designation | text |
| `max_site_coverage_pct` | Max site coverage (IZ) | % |
| `max_far` | Max floor area ratio (II) | |
| `max_height_m` | Max building height | m |
| `max_floors` | Max number of floors | text (e.g. P+4+Pk) |
| `building_line_m` | Building line (setback from public area) | m |
| `setback_neighbours_m` | Min distance from neighbouring parcels | m |
| `parking_requirement` | Parking requirement | text |
| `min_green_area_pct` | Min green area | % |
| `planned_parcel_area_m2` | Planned parcel area (per plan) | m² |
| `utilities` | Infrastructure utilities | text |
| `max_gfa_m2` | Calculated max gross floor area (BGP) — `accent` row | m² |
| `max_coverage_area_m2` | Max coverage area | m² |

Wireframe row labels for the same values (use the API's `label_en` / `label_me`; these are the
mock's): Land use designation, Max building height, Max site coverage (IZ), Floor Area Ratio (II),
Planned parcel area, Max Gross Floor Area, Max coverage area.

**Group 2: market data & feasibility** (shown to everyone: no subscription, badge or lock in the
POC), **always low / expected / high ranges, never single figures**: estimated land
value (€), construction cost (€), design & documentation (€), estimated market value (€),
estimated saleable area (m²), potential profit (€), return on investment (%); plus the cost rows
land value, design & documentation, construction, total cost. ROI hero (44 px figure + "range
lo% — hi% · expected n%"), range rows with the brand bar, "◐ Test your own assumptions" sliders
(construction €/m², sale price €/m², saleable %; 70 % default, visible and editable), disclaimer
"Figures are indicative ranges from Realitica, Estitor & Monstat — not investment advice.
Deterministic calculation; AI does not generate financial values." Formulas are client-owned and
deterministic (shared engine `@urbanview/feasibility-engine`, `formula_version poc-1`,
`client_validated: false`): GFA = FAR × plot area; coverage area = coverage % × plot area;
profit = GFA × saleable share × sale price − (land + design + construction); ROI = profit / cost
× 100.

**CTA stack**: gold "Order expert analysis" + price (€100 ≤ 500 m², €200 above, from the API's
pricing), ghost "Unlock full market data" (urban parcel panel), ghost "Ask about this site", line
"How we analyze this parcel".

## Admin console (`/admin/*`: `src/app/(shell)/admin/`, `src/components/admin/`, `src/lib/admin/`, `src/auth.ts`, `src/proxy.ts`)

- **Routes in the shell.** `src/app/(shell)/layout.tsx` is the layout of `/` and `/admin/*`, so the
  map stays mounted under the console (the wireframe's `.admin` overlay over the main row). The
  admin pages arrive as `AppShell`'s children and render inside `AdminOverlay` when the path starts
  with `/admin`; the store's `view` mirrors the route both ways (the topbar's Map / Admin, the
  pill and ⌘K call `setView`, which navigates). Tabs are routes: `/admin/data`, `/review`
  (+ `/review/geometry`), `/publish`, `/assumptions`, `/orders`, `/analytics`, `/audit`;
  plus `/admin/users` (the account menu), `/admin/login` and `/admin/no-access`. `/admin` goes to
  the role's first tab (Documents; Orders for an expert).
- **Sign-in: magic links only (Auth.js v5, `src/auth.ts`).** The backend owns the link: the form's
  server action posts `POST /v1/auth/magic-link` with the console's language (the `uv.lang`
  cookie: the sign-in e-mail is written in that one language; always 202, same "Check your
  email" for any well-formed address: no enumeration), the backend's `magic_link` e-mail opens
  `${ADMIN_BASE_URL}/login?token=…` (ADMIN_BASE_URL = site + `/admin`), and that page exchanges the
  single-use token (15 min) once through the Credentials provider `magic-link` →
  `POST /v1/auth/magic-link/exchange` → staff bearer token → `GET /v1/admin/users/me` for the role.
  A Credentials provider rather than Auth.js's Email provider: the Email provider would mint its
  own tokens and need a database adapter; the backend already issues, stores and consumes them.
  Sessions are JWTs (encrypted, httpOnly cookie, `AUTH_SESSION_MAX_AGE`, 24 h). The backend token
  stays inside the JWT: the `session` callback (what `/api/auth/session` returns) carries name,
  e-mail and role only; server code reads the token with `lib/admin/session.ts`
  (`staffApiToken`, Auth.js `decode` on the session cookie). The role is re-read from `/me` every
  5 minutes (a changed role or a deactivated user takes effect; 401 = signed out). Sign-out (server
  action) clears the cookie and the `signOut` event revokes the backend session
  (`POST /v1/auth/sign-out`). Env: `AUTH_SECRET` (server only), `AUTH_URL` / `AUTH_TRUST_HOST`
  behind Caddy, `API_INTERNAL_BASE_URL`.
- **Open access (temporary, `lib/admin/open-access.ts`, off by default).** While the server cannot
  mail the links (no SMTP details yet), `ADMIN_OPEN_ACCESS_TOKEN` (server only; an admin entry of
  the API's `ADMIN_API_TOKENS`) skips the sign-in: the proxy, the pages and the server actions take
  every visitor as the admin "Open access" (`currentStaff`), every console call carries that token
  (`consoleApiToken`), the sign-in page redirects to the console, the account menu has no sign-out
  and a refused token is an error, not a redirect loop (`api.ts`). Unset = the magic links above,
  unchanged. Switch it off once `SMTP_HOST` is set.
- **Roles** (`lib/admin/sections.ts`, the one table the proxy, the tab row and the pages read;
  the pilot technical scope's, auth check 2026-09-29): admin = every tab + users; reviewer
  ("planning expert approving extractions") = Documents (read: no upload, register, job or live
  buttons, `DataReadOnly` context), AI review queue, Publish;
  expert ("produces paid reports") = Orders only (the API returns only the orders assigned to
  them; the report upload is their only action). Reviewers have no order access. Guards: `src/proxy.ts` (Next 16's renamed
  middleware; no session → `/admin/login?callbackUrl=`, a section outside the role → a rewrite to
  the plain "You don't have access to this section" card under the same URL), each page's
  `guard(section)`, and the API's own 403 on every `/v1/admin/*` route.
- **Data** comes from the Next server (`lib/admin/api.ts` `adminGet`: the staff bearer token, 401
  → sign-in, 403 → no access); nothing staff-only reaches the browser. Orders (below);
  Audit log (admins) `GET /v1/admin/audit`, one table, newest first (time, actor, action,
  entity, before → after as the changed keys), filters action prefix and actor as a GET form, 50
  per page; **Analytics** (`/admin/analytics`, admins; `lib/admin/analytics.ts`, no mock screen:
  plain tables, no dashboard) `GET /v1/admin/analytics` with a from / to GET form (default the
  API's last 30 days; `to` inclusive on the form, sent as the exclusive next day): the funnel
  (map loaded → picked a parcel → opened its panel → started an order → placed an order → paid:
  step, counted from, sessions, from previous, from start), orders by status (status, orders,
  amount), most-searched districts (district, Covered / No adopted plan chip, searches, outside
  coverage, parcel picks, sessions, share; searches outside coverage count for the district of
  their point, a point in no zone is "Outside every district"), searches outside coverage by
  position (≈ 110 m), and repeat visitors (3+ sessions) / the two intent counts; Users
  `GET /v1/admin/users`; Financial
  assumptions (below). Action buttons are hidden for
  read-only roles.
- **AI review queue** (`/admin/review`; `components/admin/review/*`, rules in
  `lib/admin/review.ts`, calls in `lib/admin/review-actions.ts`): the wireframe's card ("AI
  extraction — review queue", "100% of extracted values need expert approval before they
  publish", "N pending"), a progress header (n of N reviewed for the document in view, counters,
  Publish for admins and reviewers once the document has nothing pending — until then a "n pending before
  publish" chip with a tooltip — the job's step while it runs, the data version the map serves,
  "Corrected values reach the map only after a
  publish"), the switch "Extracted values · n pending | Geometry · m pending" (`review-tabs.tsx`;
  the geometry count from `GET /v1/admin/geometry?limit=1`), filters (document with its pending
  count, status, zone, target type, page, order:
  pending first / page then parcel / low confidence first; `?document=&file=` from Data sources),
  then three panes that scroll on their own: the queue (the mock's `.review-item` rows: "Max floor
  area ratio (II) — UP 12", "DUP … · p.14", the value chip; ⚑ low confidence), the item (value, AI
  value struck through next to a correction, parameter labels, unit, target, the raw text, the
  staged payload (`payloadLines`: as printed, normalised with its rules, floors, land-use class,
  table cell), confidence and the checker's flags, source page and file, the run's job and cost,
  the last
  decision's actor, time and note (the whole trail is the admins' Audit log); one decision per
  item, no bulk approval) and the cited page (`components/source/pdf-page-view.tsx`: PDF.js
  from the item's signed link, the value's box, prev / next, zoom, fit, "Cited p.N", Open PDF; a
  document is loaded once per file, `lib/pdf.ts`). Keyboard: j / k or ↓ / ↑ move, Enter approves
  and moves to the next pending item at once (sent in the background, undone with a toast if
  refused), e amends (an editor per parameter: a number with its unit, the floor notation "P+5+Pk",
  a land-use designation from the document's own wordings or typed, free text; a note is
  required; the API checks it with the extraction contract's rules and its refusal names the rule
  (`correctionRefusal`): a number outside the field's usual range shows "The plan really says N:
  keep it" and saves only when ticked, `confirm_out_of_range`), r rejects (reason required), n next
  pending, Esc closes, Ctrl+Enter saves. Toasts
  "Approved", "Amended", "Rejected"; refusals in plain words, never red. The queue keeps its own
  state (200 items a page, "Load more"); decisions answer the item and the document's counters.
  Styles: block 18 of `overrides.css`. Tests: `lib/admin/review.test.ts`.
- **Geometry review** (`/admin/review/geometry`, admins and reviewers, the pilot scope's A2
  geometry drafts; `components/admin/review/geometry-screen.tsx`, `geometry-preview.tsx`, rules in
  `lib/admin/geometry.ts`, calls in `lib/admin/geometry-actions.ts`; no mock screen: the value
  queue's card, panes, rows and keys): the card ("Geometry — review queue", pending chip, the
  switch, the counts line "n pending · n approved, waiting for the next publish · n rejected ·
  n failing their checks", filters status / origin / layer / include published), then three panes:
  the batches (pending first, failing QA first; title = layer — document short code / name / run,
  source line = origin · run · features, QA and decision chips), the batch (origin and run, decision
  and QA chips, the last decision, Approve (Enter; disabled with the reason when its QA fails),
  Reject (r, reason required, final; one decision per batch), the checks: every issue
  with its chip, sentence, count and the features it names; facts: origin with its meaning, layer,
  document link, producing run, features, staged, the georeferencing fit and snapping, published)
  and the preview (`projectFeatures`: the batch's simplified features as an SVG, north up, fitted to
  the pane; features an issue names outlined in the danger colour, a feature named
  on hover). Keys j / k, Enter, r, n, Esc. Styles: block 23 of `overrides.css`. Tests:
  `lib/admin/geometry.test.ts`.
- **Publish** (`/admin/publish`, admins and reviewers, the pilot scope's A4;
  `components/admin/publish/publish-screen.tsx`, rules in `lib/admin/publish.ts`, calls
  `publishAction` / `publishStatusAction` in `lib/admin/review-actions.ts`):
  what the map serves (`v7 · label`, when, who), "Publish" with an optional label and notes
  (disabled while any document has pending items or any geometry waits for review, `blockersText`,
  named with
  a link into the review queue or the geometry review), the
  running job's steps (preflight … prune, `stepLabel`, read every 2 s until it ends; a failed
  last run says where it stopped, its error cut at a word with the full text on hover,
  `shortError`), and every version (`version_no` + label + Live chip, published when and by
  whom, what it holds: values, parcel links, heatmap cells, the tiles key and size or "cleared").
  Earlier versions keep their tiles (retention) for a manual pointer flip by an operator; there
  is no rollback button or endpoint. Tests: `lib/admin/publish.test.ts`.
- **Users** (`/admin/users`, admins; `components/admin/users/users-screen.tsx`, `lib/admin/users.ts`,
  `lib/admin/user-actions.ts`): "Add staff" (work e-mail, name, role; they sign in by e-mailed
  link, no password) over `POST /v1/admin/users`, and the staff table with a role select and
  Activate / Deactivate (confirmed; closes their sessions) per member over `PATCH
  /v1/admin/users/{id}`; the signed-in admin's own row is not editable (the API refuses too).
  Every write is audited by the API (`user.create`, `user.update`). Tests: `lib/admin/users.test.ts`.
- **Documents** (`/admin/data`, `/admin/data/documents/[id]`; `components/admin/data/*`, rules
  in `lib/admin/data.ts`, writes in `lib/admin/data-actions.ts`): the wireframe's sources card
  (Source / Provides / Format / Status, "+ Upload document"), its rows the municipality profile's
  `sources` (`sourceRows`), each status the source's real integration (`integrationChip`:
  Manual upload, File import, Reference copy, Access pending … "Linked" only for a live
  connection; the note under the chip; A1 check 2026-09-29, never the mock's blanket "Linked"),
  and the planning documents (the card names the municipality): one row
  per current version (short code chip + name → its page, zone, type, status, version, files,
  overall state, coverage, actions Queue extraction / Queue geometry / Mark live · not live / New
  version…) with one indented row per file (extraction and geometry: queued / running / succeeded
  / failed with attempts, cost, when it ran — "finished 3 min ago", the exact UTC times on hover,
  `jobTimes` — and the reason; a PDF with raster sheets "needs QGIS redraw · p. 3", `redrawText`;
  Retry, Extract, Rerun, "Review n →" = `/admin/review?document=&file=`). Filters zone / status /
  state / job state / name in the URL (`next/form`). "Zones from QGIS" (admins,
  `zone-import.tsx`): drop the zone GeoPackage (uploaded like any file), "Check only" or "Import
  zones" (`POST /v1/admin/zones/import`), the latest imports with their outcome (staged dataset
  version and counts, passed, or refused with the errors); no zone editor. The
  document page: facts (with short code and municipality), "Edit" (admins, current version:
  status, name, short code, zone, adoption date, source, registry link, licence note; `PATCH
  /v1/admin/documents/{id}`, a note on what a status change does), "Add as" text / drawing / both
  + drop zone (PDFs, several at once, and as a drawing GIS files too: `kindsForRole`; each file
  uploads, joins the version and, unless a drawing, is queued for extraction), the
  files table (role select, pages, the pages to redraw in QGIS, extraction, cost, geometry, Remove disabled
  with the API's reason once an item was approved), the Georeferencing card (the latest
  `georeference` run: status, RMSE against the limit, max residual, snapping, cadastral overlap
  and mean offset, warning codes, one row per sheet with its RMSE; "Not georeferenced yet" with
  the CLI to run otherwise; a GIS drawing staged by the geometry job says "GIS drawing in its own
  coordinate system … no control points needed") and the version history. "+ Upload document"
  takes PDFs, GIS files and cadastral extracts (kind from the extension, editable), a progress
  bar each; a known checksum is "Already uploaded" with a link to its document, never an error;
  PDFs and GIS drawings continue to "Register a planning document" (name, short code, type from the profile, DUP / PUP / PGR
  first, status, zone, adoption date, source default eRegistri + registry link, licence /
  permission note, files with roles; "New version…" starts from the current version and shows
  the history). Uploads go through `app/api/admin/files/route.ts` (`lib/admin/upload-proxy.ts`,
  shared with the order report upload: same-origin, staff role, multipart streamed to
  `POST /v1/admin/files` with the staff token; XHR for progress; server actions cap bodies); every
  other write is a server action that answers `{ok, message}` (toast)
  and re-reads the page (`revalidatePath`), forms keep what was typed on failure. `AutoRefresh`
  re-reads the page every 3 s while a job is queued / running and stops after. Styles: block 17
  of `overrides.css`. Tests: `lib/admin/data.test.ts`.
- **Orders** (`/admin/orders`, `?order=<id>` opens the drawer; `components/admin/orders/*`, rules
  in `lib/admin/orders.ts`, writes in `lib/admin/order-actions.ts`): the wireframe's card
  ("Expert analysis orders", "Manual fulfilment queue"), filters status / expert (managers) /
  search (reference, e-mail, name, parcel) in the URL (`next/form`), the table, newest first
  (Ref, Parcel `#1042 · Podgorica I` with "urban parcel UP 12" under it (`parcelCells`, the API's
  `ko_and_number` / `planned_parcel`), customer · company + e-mail, Placed (relative), Days,
  Status chip + "⚠ e-mail" when an e-mail failed, Price, Turnaround (business days, the expected
  date on hover), Data (the version the customer saw), Delivered, Expert, Open). The drawer (fixed right, 600 px,
  a scrim closes it): Customer (an individual's name, or company, PIB and the name; the contact
  person and registered address of orders placed before migration 0031; e-mail, telephone, the
  customer id, message); Location ordered (cadastral parcel, urban parcel, the panel it was
  ordered from) with "Open on the map ↗"
  (`/?parcel=<cadastral_parcel_id>`: today's published data, the snapshot below is what was
  shown); Price and turnaround; **Payment** (admins): "Mark payment received"
  (amount, date, bank reference, all required; also on a failed payment), "Payment not
  received" (a note: the order becomes `Payment not received`, chip `rev`, which the customer's
  order page shows with the instructions; again on a failed order it only records the check),
  "Refund" (amount, date, reference; also after delivery), each confirmed in a
  line before `POST …/payment` is sent; **Fulfilment**: the expert picker (active experts with
  their orders in progress, `GET /v1/admin/orders/experts`) + Assign / Reassign (only once the
  order is paid: work starts; the API refuses earlier), the report (download, `vN`) and its
  upload (`report-upload.tsx`: drop zone,
  progress, `app/api/admin/orders/[id]/report/route.ts`; the customer is e-mailed the link;
  replacing a delivered report needs a note); E-mails (template, status chip, recipient, time,
  error / suppression reason); "What the customer saw" (the snapshot, read-only: data,
  market and formula versions, planning values with their pages, the assumptions with "changed
  by the customer", the Group 2 ranges: the payload's figures plus the cost rows they do not
  repeat, land value, design & documentation and total cost, in whole euros,
  `snapshotFeasibility`); Timeline (the order's audit entries in plain words,
  `eventLine`, with actor and note). Every button follows the API's status flow
  (`allowed(order, action, role)` → visible / enabled / reason, the tooltip of a disabled one);
  an expert gets the report upload only (payment and assignment are not rendered, and the API
  answers 403). Styles: block 19 of `overrides.css`. Tests: `lib/admin/orders.test.ts`.
- **Financial assumptions** (`/admin/assumptions`, admins; `components/admin/assumptions/*`,
  rules in `lib/admin/assumptions.ts`, writes in `lib/admin/assumption-actions.ts`): the
  wireframe's card ("Financial assumptions — Benchmarks by district…", "Save changes") over one
  row per zone (district; zones from `GET /v1/zones`, versions from `GET
  /v1/admin/assumptions?include_history=true`): Land, Construction, Design & documentation and
  Sale €/m² as inputs (bounds or "range ±" under each), Saleable % (blank = 70 %), Range ± (−low %
  / +high %: the factors), the source note, and in the District cell the live version ("v2 Live
  since 26 Sep 2026"), any scheduled one ("v3 Scheduled from 1 Oct 2026") and "Changed — will be
  vN". The bar above: "Applies from" (the municipality's today by default, `min` today; the note
  says "Applies today…" or "Scheduled: the panel keeps today's figures until …") and the current
  formula version (`GET /v1/admin/formulas`: label, since when, the client's approval note on
  hover) with the engine version. "Save changes" checks every changed row the
  API's way (`checkDraft`: positive figures, both bounds or neither, low ≤ expected ≤ high, a
  source; marked cells, the first bad row opened), shows a confirmation line (zones, new versions,
  date) and sends one `POST /v1/admin/assumptions/batch` (all or nothing); failures keep every
  figure. "Details" opens a row under the district (`zone-detail.tsx`): the absolute low / high per
  rate and the notes; the version history (version, status chip, applies from, until (the API's
  `effective_to`), author, time, "View diff vs vN" = `diffVersions`). Drafts start from the live version; the page
  is keyed on the versions, so a save remounts it with fresh drafts. Nothing is deleted.
  Styles: block 20 of `overrides.css`.
- **Components** (`components/admin/parts.tsx`, the wireframe's markup): `AdminCard` (`.card >
  .cardhd` h3 + `.sub` + right-side action), `DataTable` (`.tbl`, `.mono` cells for numbers and
  references), `StatusChip` (`.st.ok | .pend | .rev`), `AdminButton` (`.abtn`, `ghost`, `sm`),
  `NoAccess`, `AdminUnavailable`; `admin-frame.tsx` (the bar: title, tabs
  the role may open, account button, "← Back to map"), `account-menu.tsx` (not in the mock: role
  chip + name, menu with the e-mail, Users for admins, sign-out; fixed-positioned
  because `.adminbar` scrolls horizontally, and compact so the bar fits at 1440 px), `sign-in.tsx`
  ("Send magic link" → "Check your email"; the link state "Signing you in…" or the invalid-link
  note). Words and chips of the tables: `lib/admin/format.ts`. Styles not in the mock: block 16 of
  `overrides.css`.
- First admin on a server: `python -m core.staff login-link --email <e-mail> --create --role
  admin` (backend) prints a one-time sign-in link (works without SMTP; `deploy/README.md` step 6).
  Tests: `lib/admin/sections.test.ts` (tabs per role, guard decisions, callback URLs),
  `lib/admin/format.test.ts`, `lib/admin/data.test.ts`, `lib/admin/review.test.ts`,
  `lib/admin/geometry.test.ts`,
  `lib/admin/orders.test.ts`, `lib/admin/assumptions.test.ts`; backend `tests/integration/test_admin_console_postgis.py`,
  `test_document_files_postgis.py`, `test_review_queue_postgis.py`,
  `test_orders_console_postgis.py`, `test_assumptions_schedule_postgis.py`.

## Rules the frontend must keep

- **Uncovered is not an error.** A location outside coverage answers 200 `covered: false`: hide
  the panel, show the "Outside current coverage · no adopted plan" pill, keep the map navigable.
  A parcel reference matching nothing is a neutral inline row in the search. Only malformed
  input is a 422, and a stale entity link (404 on `/v1/panel`) returns quietly to the map.
- Numbers arrive raw (`_pct` 0–100, `_share` 0–1); format per language on the client. Text comes
  bilingual (`_en` / `_me`).
- **Analytics** (`src/lib/analytics`): anonymous `client_id` (persistent) and `session_id`
  (persisted with its last activity; new after 30 min idle) in `localStorage`, both UUID v4
  (`randomUuid`, the pilot scope's `session_id uuid`; no cookie); batches of ≤ 50 to
  `POST /v1/events`, flushed every 4 s, when full, and on page hide (`keepalive`); `map_loaded`
  when the map has loaded (see "Map"), `return_visit` (+ `days_since_last`) and `sessions_per_user` when a session starts;
  a 4xx batch is dropped, network / 429 / 5xx retried with backoff. **Never** personal data in
  properties (the API rejects name, email, phone, address … keys and e-mail / IP-looking values).
  The 13 event names are the API's enum (BRD §6.2's eleven + `market_data_interest`,
  `ai_interest`). A batch is judged row by row: malformed rows come back in `rejected`, the valid
  ones are stored.
- The selection flow (`src/lib/selection.ts`): map click (feature or empty), address suggestion,
  zone suggestion, KO + parcel number or `?parcel=` link → `/v1/locate`, `/v1/locate/parcel`
  or `/v1/parcels/{id}/panel` (cached under `queryKeys`, where panels read it) → selection +
  highlight + pin (+ fly for searches) or S6, and `search_performed {search_kind, matched}`
  with where it landed (`lat` / `lng`, ids).
  The search calls answer an outcome (`found` / `not_found` / …) instead of toasting.
- No secrets in `NEXT_PUBLIC_*`; the Mapbox token is a public token by design.
- **API client** (`lib/api/client.ts`): base URL, `X-Request-ID` per request, the error envelope
  → `ApiError`, timeouts; in the browser every call also carries the anonymous analytics session
  id as `X-Session-ID` (`setSessionIdProvider`, registered by `lib/analytics/react.tsx`; the
  first call starts the visit if the page has not yet; never sent from the server, never with
  analytics off; the API logs it on the access line). A 429 is retried by the client at most
  twice after `Retry-After` (else 0.5 s, 1 s) when that wait is ≤ 10 s; a longer one (the order
  cap per e-mail and day) is the caller's answer, and React Query does not retry a 429 again.
- **Language** (`lib/i18n/`): the shell's strings live in one table (`strings.ts`, `en` = the
  wireframe's copy verbatim, `me` = Montenegrin **drafts** until the client approves them; the
  `me` table must carry every key, `strings.test.ts` checks placeholders): topbar and search,
  layer rail and cards, legend, map chrome and the coverage pill, the empty panel, the "Ask about
  this site" acknowledgement, the shell's toasts and the disclaimer footer. Components call `useT()` (`t("nav.map")`,
  `t("nav.returnTo", { name })`); code outside React `tNow()`; pure rules take their words as a
  parameter with the English default (`lib/search.ts` `SearchWords`, the legend's `LegendContext.t`,
  `priceScheme`'s `PriceWords`), so their tests stay language-free. The topbar's language button
  (not in the mock) shows the language it switches to (`ME` / `EN`); switching re-renders every
  shell string at once, no reload. The choice is the `uv.lang` cookie (a year), read by
  `app/layout.tsx` so the server renders the page in it and `<html lang>` (`en` / `cnr-Latn`)
  matches; without a choice `NEXT_PUBLIC_DEFAULT_LANG` (`en`; the setup ticket's target is `me`,
  flip it once the copy is approved). The two parcel panels follow the switch too (2026-10-02:
  identification, the comparison card, planning rows, Group 2, the sandbox, the buttons, the
  shared loading / retry / data-version lines; sentences with a bold figure are filled with
  `fillNodes(t(key), { area: <b>…</b> })`; dates with `formatDate(iso, lang)`); the zone and
  document panels and the modals (order form, methodology, engine, source viewer) keep English
  for now; API text that comes bilingual picks its side with `pickLang(lang, en, me)`. Settings the server needs are in
  `lib/i18n/config.ts` (the client module cannot be called from a server component).
- **Disclaimer footer** (`components/ui/disclaimer.tsx`): the wireframe's note under the figures
  (string table) until the API's `disclaimer_status` is `client_approved`, then the API's
  `disclaimer_en` / `_me` in the shell's language; the market section uses it.

## Code map

| Path | What |
|---|---|
| `src/app/layout.tsx` | fonts, CSS order (wireframe → overrides), providers |
| `src/app/page.tsx` | server-reads `/v1/municipality` (1.5 s timeout, never blocks) → `AppShell` |
| `src/components/shell/*` | `app-shell` (frame, ⌘K, intro toast, `?parcel=` sync), `topbar`, `search-box`, `layer-rail`, `legend`, `map-view` (Mapbox + PMTiles, click / hover / highlight, `map_loaded`), `map-chrome`, `info-panel` (selection → panel variant, empty state), `methodology-modal`, `engine-modal` ("How the figures are calculated"), `admin-overlay` (the `/admin` routes' container), `hosts` (modal + toast) |
| `src/components/panel/*` | S3 panel variants: `zone-panel`, `document-panel`, `cadastral-panel`, `urban-panel`, `panel-parts` (header, loading / unavailable, status chip, `panel_viewed`, meta and height text), `parcel-parts` (comparison card, row source icon, parcel CTA stack, zone type), `market-section` (Group 2 figures), `assumption-sandbox` ("◐ Test your own assumptions") |
| `src/lib/assumptions.ts` | the sandbox's sliders, bounds and validation, and the live recalculation through the shared engine package (`recalculateFeasibility`) |
| `src/lib/order.ts` | `requestOrder`: every order button → the S4 modal for the parcel on screen, `order_started` |
| `src/lib/order-form.ts` | the order form's rules: draft, validation (the API's), request body, failure sentences |
| `src/components/order/*` | `order-modal` (S4), `order-confirmation` (S5 + bank-transfer instructions), `order-status` (the public order page) |
| `src/app/orders/[reference]/page.tsx` | the public order page route |
| `src/lib/pricing.ts` | the order price of a parcel from the configured tiers (`GET /v1/orders/pricing`), the server's `price_for` rule |
| `src/lib/map/*` | `style` (UrbanView layers per registry entry, visibility, choropleth paint, highlight filters), `classes` (choropleth colours, legend rows, expressions from served classes), `pick` (click priority, centroid), `tiles` (pointer → source, provider registration), `camera` (fit options, padding for the box), `use-layer-states` (every card's `layerState` for the rail and legend), `pmtiles-provider` (worker module), `provider-name` |
| `src/components/ui/*` | shared: `LayerCard`, `PanelRow`, `IdGrid`, `Cta` (primary / gold / ghost / line), `SourceRef`, `Modal` + `ModalHead` (Radix Dialog with wireframe classes), `Disclaimer`, `icons` |
| `src/lib/api/*` | `client.ts` (fetch wrapper: base URL, `X-Request-ID`, `X-Session-ID`, error envelope → `ApiError`, timeouts, 429 retries), `endpoints.ts` (one function per route), `hooks.ts` (React Query: `useMunicipality`, `useLocate`, `useLocateParcel`, `useGeocode`, `useZones`, `usePanel`, `useSourceValue` / `useSourcePage`, `useTilesCurrent`, `useCreateOrder`, `useOrderStatus`, `useTrack`), `types.ts` (aliases), `schema.d.ts` (generated) |
| `src/lib/store.ts` | shell state (zustand): rail, layers, view, selection (point / parcel / feature / zone), pin, toast, modal, map controller |
| `src/components/source/source-viewer.tsx` | the source viewer: signed link → PDF.js page (lazy), bbox highlight, pages, zoom, Open PDF, retry, `source_reference_opened` |
| `src/lib/source.tsx` | `useOpenSource`: opens the source viewer for a value or a document page (with a `hint`) |
| `src/lib/source-text.ts` | the source reference label (`sourceRefText`), the viewer's failure words (`sourceFailure`) and page-range note |
| `src/lib/search.ts` | S2 rules: parcel + KO parsing, zone matching, zone of an address hit, suggestion rows, recent searches |
| `src/lib/layers.ts`, `format.ts`, `selection.ts`, `storage.ts` | layer catalogue (+ `hasNoData`), formatters, selection flow, safe `localStorage` |
| `src/lib/i18n/*` | `strings.ts` (the shell's string table en / me, `translate`), `index.tsx` (`LangProvider`, `useLang`, `useT`, `tNow`, `pickLang`), `config.ts` (cookie, default, `<html lang>`; server-safe) |

API types are generated, never hand-written: in `backend/` run `python -m api.export_openapi`
(writes `frontend/openapi.json`), then `npm run api:types`.

## Commands

From `frontend/` (or the root with `-w @urbanview/frontend`): `npm run dev`, `build`, `start`,
`lint`, `typecheck`, `test` (vitest), `design:check`, `api:types`, `check` (design + types + lint
+ tests). `predev` / `prebuild` generate `public/map/urbanview-pmtiles.js` and copy the PDF.js
worker to `public/pdfjs/`. Env: `.env.example` (`NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_MAPBOX_TOKEN`,
`NEXT_PUBLIC_MAPBOX_STYLE`, `NEXT_PUBLIC_ANALYTICS_ENABLED`, `NEXT_PUBLIC_DEFAULT_LANG`, optional
`API_INTERNAL_BASE_URL`, and for the admin console `AUTH_SECRET`, `AUTH_SESSION_MAX_AGE`). Server image: `frontend/Dockerfile` (build context = repo root,
`NEXT_OUTPUT=standalone` switches `next.config.ts` to a standalone server traced from the root;
`NEXT_PUBLIC_*` are build args; `deploy/README.md`). Routes: `/` (the map), `/?parcel=<Parcel ID>`, `/admin/*` (the admin
console, see its section), `/orders/<reference>` (the
public order page, the link of the confirmation and the order e-mails).
Without a Mapbox token the map area shows the wireframe background and the chrome only.
