# Frontend design: the wireframe is the exact target

Status: design contract for the public map (`frontend/`) and, for its chrome, the staff tool
(`admin/`). Decided by the product owner on 2026-09-24: **the frontend reproduces the interactive
mockup "UrbanView — Urban feasibility engine" exactly.** This supersedes the BRQ remark that the
mockup is a reference "not strictly" to follow. Same tokens, fonts, dimensions, spacing, copy,
states and interactions; the only permitted deviations are the ones listed in §9 (real map
library, real data, and the POC scope trims from `docs/UrbanView_POC_Exclusions.docx.md`).

"Exact" is measured against the files in §1: the stylesheet is ported verbatim, the screenshots
are the acceptance references, and `computed-styles.json` gives the effective value of every
property the stylesheet's override passes touch. Do not re-derive values from the first
declaration in the CSS: the file ends with three override passes ("luxe pass", "type
consolidation", "font roles") that change most sizes and radii (§4, §5).

## 1. Reference files (`docs/wireframe/`)

| File | What it is | Use it for |
|---|---|---|
| `UrbanView New Design.html` | The mockup as delivered: a self-unpacking bundle (manifest of gzip + base64 assets: 2 SVGs, 8 woff2 fonts). Needs JS to open. | Provenance only. |
| `wireframe-decoded.html` | The unpacked page: head, `@font-face` block (asset URLs are bundle ids and do not resolve), the app stylesheet, the app script. Opens directly with fallback fonts. | Reading the whole thing in one file. |
| `wireframe.css` | **The app stylesheet, verbatim** (711 lines; the second `<style>` block of the decoded page). | Port as the global stylesheet, unchanged (§2). |
| `wireframe.js` | **The app script, verbatim** (1 578 lines): mock data, every render template (topbar, rail, legend, map, panels, modals, AI panel, admin console), every copy string and interaction. | Markup structure and class names per component; copy strings (§7); interaction rules (§8). |
| `screens/*.png` | One screenshot per app state at 1440×900 (plus three narrow widths), rendered by `make_screens.py` with the real fonts, the brand SVGs and a seeded cadastre. | Acceptance references, state by state (§6). |
| `computed-styles.json` | `getComputedStyle` of 257 selectors (font, size, weight, tracking, colour, background, radius, shadow, padding, box size) captured across the states, plus the resolved `:root` tokens. | The effective value of anything ambiguous in the CSS. |
| `make_screens.py` | Builds the harness (Google Fonts, `../brand` SVGs, seeded `Math.random`, hash-driven states) and runs headless Chrome/Edge. `python docs/wireframe/make_screens.py [state…] [--dump]`. | Regenerate screens / computed styles after a mockup update. |
| `../brand/UrbanView_logo.svg`, `../brand/UrbanView_mark.svg` | Combination mark (topbar, 36 px high, rendered white with `filter: brightness(0) invert(1)`) and brand mark (empty panel, 60 px inside a 96 px circle). | Assets. |

Fonts: **Schibsted Grotesk** 400 / 500 / 600 / 700 / 800 and **JetBrains Mono** 400 / 500 / 700
(the eight woff2 files in the bundle; the harness loads the same faces from Google Fonts; the
production build self-hosts them). `--disp` and `--body` are both Schibsted Grotesk; the comments
in the CSS that mention "Instrument Serif" and "Geist" describe a pass that was never applied and
must be ignored. No serif is loaded anywhere.

## 2. How to port it exactly

- **Global stylesheet = `wireframe.css` verbatim.** Keep the order, the override passes and the
  odd-looking rules: `[class][class][class],input,select,textarea,button,…{border-radius:8px}` is
  load-bearing (it gives every element that has a `class` attribute an 8 px radius, whatever the
  earlier rule said); the `.pill[class][class]` … `999px` and `.chk[class][class]` … `50%` rules
  restore pills and circles. Cleaning the file up changes the design.
- **Keep the class vocabulary and the markup structure** of the templates in `wireframe.js`:
  a React component renders the same elements with the same classes as its template
  (`.phead > .pclose + .peyebrow + .ptitle + .psub`, `.idgrid > .idcell > .k + .v`, `.prow > .pk +
  .pv > .u`, and so on). This is what makes the CSS apply unchanged.
- Tailwind may be used for layout glue that has no visual effect; shadcn/ui only for behaviour
  (dialog focus trap, menus) with the mockup's classes on the rendered elements. Do not restyle a
  component with utilities when the stylesheet already styles it.
- `body { overflow: hidden }`; `.app` is a `100vh × 100vw` flex column; nothing scrolls except
  `.pscroll` (panel), `.rail`, `.legend`, `.aibody`, `.mbody`, `.adminbody`.
- Inline SVG icons are part of the design (15 × 15 line icons, stroke 1.2–1.5, `currentColor`);
  copy them from the templates rather than substituting an icon set.
- Numbers use `font-variant-numeric: tabular-nums` through `.mono`; money is `€` + `en-US`
  grouping in the mock (`€1,650`); the real app formats per language (`panel-payload.md`).

## 3. Tokens

Verbatim `:root` of `wireframe.css` (resolved values in `computed-styles.json → tokens`):

| Token | Value | Role |
|---|---|---|
| `--ink` | `#2A2118` | primary text, dark surfaces (ROI hero, coverage pill, toast, AI fab / head) |
| `--ink-2` | `#6B6152` | secondary text, row keys, legend rows |
| `--ink-3` | `#A79E8C` | tertiary (declared, unused by the mock) |
| `--paper` | `#F4EFE6` | page background, CTA stack, modal footer, cards inside the panel |
| `--paper-2` | `#ECE4D6` | dividers inside lists, range-bar track, scrollbar thumb, icon tiles |
| `--line` / `--rule` | `#DDD3C3` | every hairline border |
| `--white` | `#FBF8F2` | rail, panel, legend, modal, cards |
| `--brand` | `#B5613B` | terracotta: primary actions, selection, eyebrows, free badge, focus ring |
| `--brand-dark` | `#8F472A` | hover of brand, planned-parcel outlines, "on" layer names, computed GFA |
| `--brand-tint` | `rgba(181,97,59,.11)` | tinted chips and hovers |
| `--paid` | `#B08A2E` | gold: everything paid (order CTA, subscription badge, lock, urban-parcel card, pill) |
| `--paid-dark` | `#8C6B22` | declared, unused |
| `--paid-tint` | `rgba(176,138,46,.13)` | gold tints |
| `--danger` | `#A24A32` | declared, unused |
| `--z-res` `--z-com` `--z-mix` `--z-pub` `--z-grn` | `#B5744A` `#BE9A44` `#8A7A8E` `#5E8A82` `#7C8A4F` | zone types Residential, Commercial, Mixed use, Public / institutional, Green / recreation |
| `--shadow` | `0 1px 2px rgba(42,33,24,.05), 0 1px 3px rgba(42,33,24,.07)` | map buttons, doc items, admin cards |
| `--shadow-lg` | `0 12px 32px -8px rgba(42,33,24,.20), 0 2px 6px rgba(42,33,24,.07)` | legend, suggestions, AI panel, modal, toast, coverage pill, AI fab |
| `--r` / `--r-lg` | `8px` / `12px` | declared; effective radius is 8 px everywhere (§5) |
| `--mono` | `'JetBrains Mono', monospace` | data, labels, badges |
| `--disp` / `--body` | `'Schibsted Grotesk', -apple-system, sans-serif` | everything else |

Literal colours used outside the tokens: topbar `#241B12` (border-bottom `rgba(244,239,230,.10)`);
page outside the map `#14110E` (bundle shell only); map background `#EDE6D6`; SVG paper
`#EEF1F5`; uncovered area `#E6E9EE` stroke `#DCE1E8` (flashes `#d0c9b8` then `#ded9cc` when a
search lands outside coverage); river `#BCCEC8` at .85, river line `#A4B9B2`; roads `#FFFFFF` 9 px
major, `#F2EBDC` 4.5 px minor; muted outlines and block boundaries `#B3A894`; public ownership
`#4F6D82`; restitution `#9E5568`; land-use swatch `#b98a5a`; planned traffic `#5b5b5b`; FAR heat
`rgba(198,107,74, 0.15 + far/3.4 × 0.55)` (multiply blend), heat swatches `#EFE3CE→#B4744A` and
`#F1E7CF→#B5853F`, legend gradient `#F1E7D6→#B5613B`; price bands `rgba(150,145,132,.34)` not
saleable, `rgba(201,154,46,.26 / .45 / .62 / .82)` for < 1 300, 1 300–1 700, 1 700–2 100, ≥ 2 100
€/m²; review status `.st.rev` `#e7ecf3` / text `#B3A894`; **`#6D28D9`** (a purple) as the hover
of `.cta.gold`, `.lockcta .b` and `.lockover .b`. That purple is in the delivered file; it is
reproduced as-is until the product owner says otherwise (open item, §10).

## 4. Typography (effective values)

Base `body` 16 px Schibsted Grotesk 400, antialiased; almost every text element sets its own size.
The consolidated scale (final override pass) is:

| Role | Face / weight | Size | Tracking | Where |
|---|---|---|---|---|
| Display title | Schibsted 700 | 26 px, lh 1.1 | −.02em | `.ptitle`, `.mhead h2`, `.success h2`, `.mpane h2` |
| Empty-state title | Schibsted 700 | 24 px, lh 1.15 | −.02em | `.pempty h3` |
| Section label | Schibsted 700 | 20 px | −.02em | `.secthead .lbl`, `.adminbar .at` |
| Figure | Schibsted 700 | 44 px, lh 1 | −.02em | `.roihero .rval`, `.astat .sv` |
| Plan price / plan title | Schibsted 700 | 34 px / 18 px | −.02em | `.plan .pr`, `.plan h3`, `.cardhd h3` 18 px |
| UI chrome titles | Schibsted 600 | 15 px | −.01em | `.railhead b`, `.legend h4`, `.aihead .t h4`, `.lockover h4`; `.assum .ah` 13 px |
| Map buttons | Schibsted 500 | 19 px | | `.mbtn` (+ − reset) |
| Body | Schibsted 400 | 14 px | | `.prow .pk`, `.pempty p`, `.mhead p`, `.msg .b`, `.tbl td`, `.legrow`, inputs, `.docitem .dn` (500), `.lk2` (500) |
| Secondary | Schibsted 400 | 13 px | | the 13 px group of the CSS: `.plan .pd`, `.plan li`, `.field label` (600), `.arow label`, `.vscard .txt`, `.backlink` (600), `.mstep .ml`, `.cardhd .sub`, `.astat .sl`, `.aitrain`, `.pricenote`, `.ctx .cd`, `.methlink .mlt span`, `.lk2 .ld`, `.mpane .phase`, `.aihead .t p` (mono), `.mhead .meyebrow` (mono 700) |
| Data readouts | JetBrains Mono 700 / 400 | 13 px | | `.prow .pv` (700), `.idcell .v` (700), `.upcard .upn` (700), `.orderref` (700), `.roihero .rrange`, `.coords` (.02em), `.tbl .mono`, `.arow .av`, `.docitem .dm`, `.review-item .rextract`, `.paycard .brandmark` |
| Micro-labels (uppercase) | JetBrains Mono | 11.5 px | .16em | `.rail .rlabel` (600), `.legh` (600), `.peyebrow` (700), `.mhead .meyebrow` (700, 13 px), `.mpane .peyebrow2`, `.mrail-h`, `.roihero .rlab`, `.badge` (500, 999px), `.pill` |
| Micro-labels, lighter | JetBrains Mono 400 | 11.5 px | .08em | `.idcell .k`, `.fieldlab` (uppercase, opacity .9); `.tbl th` 700, .1em |
| Micro text | JetBrains Mono 400 | 11.5 px | | `.psub`, `.chiphint`, `.srcref`, `.securenote`, `.mnav .mcount`, `.mfoot .fnote`, `.railhead span`; `.aichip` is the same size in Schibsted |
| Status phrase | Schibsted 600 | 13 px | | `.dstat`, `.st`: a 5 px dot + word, colour brand-dark (adopted / ok), gold (in progress / pending), `#B3A894` (rev); the `.ok` / `.adopted` / `.pend` variants keep their tinted background (padding 0) |

Inline exceptions in the templates: long text values in `.prow .pv` (land use, document name,
type, source) carry `style="font-size:11.5px"`; the governing document in `.idcell .v` is 11 px;
range-bar captions are mono 9.5 px; the AI fab badge is mono 9 px; plan tags 9 px; `.pill` in the
topbar is a `<button>` and therefore takes `.topnav button` (14 px / 500, padding 8 13) plus
`.pill`'s gold colours and .16em tracking.

Selection colour: brand background, white text. Focus: `outline: 1px solid var(--brand)`, offset
2 px, plus a `0 0 0 3px rgba(181,97,59,.16)` ring. `prefers-reduced-motion` disables every
animation and transition.

## 5. Geometry, surfaces and layout

**Radius.** Effective radius is **8 px on every element that has a class** (the later "premium
 neutral" rule that asks for 12 px on modal, legend, AI panel and suggestions loses on specificity) (modal, panel cards,
ROI hero, AI fab, map buttons, chat bubbles on all four corners, legend, lock tiles, the `.lockval`
"LOCKED" chip, `.paidlock`). Exceptions: `999px` for `.pill`, `.badge`, `.chiphint`, `.aichip`,
`.srcref`, `.coverwarn`, `.plan .ptag`, `.rangebar` and its fill, `.orderref`; `50%` for `.chk`,
`.aiquota .qd`, `.success .ok`, `.mstep .mn`, `.pempty .ill`, and the empty-layer ring
(`.lyr::before`, a pseudo-element). Inputs without a class attribute: 8 px too (`.field input`,
`.searchwrap input`, `.aifoot input` are named in the rule).

**Shadows.** `--shadow-lg` on legend, suggestions dropdown, AI panel, modal, toast, coverage pill,
AI fab; `--shadow` on map buttons, doc items, admin cards and stat tiles; `.cta.primary` adds
`0 1px 2px rgba(181,97,59,.35)`; the panel, rail, `.paycard`, `.astat` (hairline only), and the
topbar have none.

**Frame (desktop ≥ 1100 px).**

| Region | Size | Surface |
|---|---|---|
| Topbar | 62 px, padding 0 20, gap 20, z 60 | `#241B12`, text paper, hairline `rgba(244,239,230,.10)` below |
| Search | flex 1, max 560 × 40, centred | input bg `rgba(244,239,230,.06)`, border `rgba(244,239,230,.18)`, placeholder `.5` alpha, padding 0 44 0 38; focus: brand border, bg `.10`, ring `0 0 0 3px rgba(181,97,59,.28)`; magnifier at left 11; `⌘K` kbd at right 9 (mono 11, border `.2`) |
| Suggestions | absolute top 44, full width of the search | white, hairline, shadow-lg, rows 12 14 with a 26 px icon tile (`--paper-2`), title 14.5, sub mono 12 ink-2 .7; hover brand-tint |
| Top nav | gap 4 | buttons 14 / 500, padding 8 13, colour `rgba(244,239,230,.74)`, hover bg `.10`; active = brand bg, white text; pill "PODGORICA · PILOT" gold |
| Layer rail | 206 px (collapsed 46, opener 34 × 34), padding 12 0 14, gap 1, scroll, z 30 | white, hairline right |
| Map | flex 1 | `#EDE6D6` |
| Panel | 392 px, z 40 | white, hairline left, no shadow; `.panel.hidden` removes it |
| AI fab | 52 × 52 at right 412 / bottom 20 (panel + 20) | ink, shadow-lg, badge brand 9 px mono "3 free" |
| AI panel | 380 × 544, same anchor, z 55; hides the map tools while open | white, hairline, shadow-lg |
| Modal overlay | fixed, `rgba(20,17,14,.55)`, padding 24, z 100 | modal max 520 (wide 860), max-height 90vh, pop animation .2s (12 px rise, .98 scale) |
| Toast | fixed bottom 24 centre, z 200 | ink, paper 14.5, padding 13 22, shadow-lg, `✦` brand prefix, slides up 80 px, visible 2.6 s |

**Layer rail.** Head "Map layers" (15 / 600) with a 26 px collapse chevron; group labels `Base`,
`Parcels`, `Context`, `Feasibility` (mono 11.5 / 600, .16em, ink-2). A layer card is a 182 × ≥ 32
button, padding 5 30 5 12, grid `auto 1fr` gap 11: 24 px swatch (radius 6, border
`rgba(42,33,24,.15)`) + name 14 / 600 ink; hover name brand-dark; **on**: name brand-dark and a
16 px brand circle with a white check at right 9; **off**: a 16 px white ring (`--line`, brand on
hover); **core** (`◆`, always on): muted circle `#B3A894` at .55, name ink-2, `cursor: default`;
**dependent** (`req`): opacity .6 and a gold `▲` at top-left while the required layer is off, plus a
`.depnote` card (182 px, 12 px brand-dark on brand tint, "Needs **Cadastral parcels** tap to turn
on") under it when it is on; **paid**: swatch grayscale .35, a 19 px gold lock at right 9 and the
sub-label "Subscription" (12 px gold, 600). Swatches: `zones` = conic gradient of the five zone
colours; `dash` = white with 1.5 px dashed brand; `docdash` = white with 2 px dashed `#B3A894`;
`heat1` / `heat2` = the two gradients; otherwise a flat colour.

| Order | id | Name | Group | Default | Swatch | Rule | Published layer |
|---|---|---|---|---|---|---|---|
| 1 | `docareas` | Planning documents | Base | on | docdash | core | `document_coverage` |
| 2 | `base` | Base map | Base | on | `#c4bdac` | core | Mapbox base style |
| 3 | `zones` | Urban zones | Base | on | zones | core; also draws block boundaries + labels and zone labels | `zones`, `urban_blocks` |
| 4 | `cadastre` | Cadastral parcels | Parcels | on | `#B3A894` | | `cadastral_parcels` |
| 5 | `planned` | Urban parcels | Parcels | on | dash | | `urban_parcels` |
| 6 | `owner` | Public ownership | Parcels | off | `#4F6D82` | requires `cadastre` | `public_ownership` |
| 7 | `restit` | Restitution / legal | Parcels | off | `#9E5568` | requires `cadastre` | `legal_burdens` |
| 8 | `landuse` | Land use | Context | off | `#b98a5a` | | `land_use` |
| 9 | `heatFAR` | FAR heatmap | Context | off | heat1 | | `block_cells` |
| 10 | `traffic` | Planned traffic | Context | off | `#5b5b5b` | | `traffic_network` |
| 11 | `heatMkt` | Price heatmap | Feasibility | off | heat2 | paid (lock) | `zone_cells` |

**Map chrome.** Legend card top-left (16 / 16): max-width 264, padding 16 19, radius 8, shadow-lg,
`max-height: calc(100% − 92px)`, scrolls; `h4` "Legend" 15 / 600 with a `–` / `+` minimise button
(opacity .5); one `.leggrp` per visible layer separated by hairlines (margin 9, padding 8); group
heading mono 11.5 / 600 .16em ink-2 .7, optional unit label (`floor area ratio`, `€/m² land`) in
sentence case; rows 14 px ink-2, margin 7 0, with a 14 px circle swatch (border
`rgba(42,33,24,.2)`), a 16 px dashed line swatch for routes, or a 26 px gradient bar; "No overlays
active" at .6 when nothing is on. Coverage pill top-centre: ink bg, paper 13.5, padding 9 18,
999px, shadow-lg, warning triangle + "Outside current coverage" + mono `no adopted plan`. Scale bar
left 16 / bottom 52: "250 m" mono 12 .7 over an 80 × 5 bracket (1.5 px ink .6). Coordinates chip
left 16 / bottom 16: mono 13 / .02em on `rgba(20,17,14,.88)`, padding 7 11: `42.4411° N ·
19.2636° E`. Map tools right 16 / bottom 88, column gap 7: zoom label mono 12 / 600 ink-2 (`1.0×`),
then three 42 × 42 white buttons (hairline, radius 8, shadow, 19 / 500): `+`, `−`, reset
(crosshair icon).

**Map styling (SVG in the mock; the Mapbox style must match these values).**

| Feature | Fill | Stroke |
|---|---|---|
| Zone | zone colour at .24 | zone colour, 1 px, .4 |
| Urban block boundary | none | `#B3A894` 1 px dashed 3 2.5, .45; label mono 700 `rgba(42,33,24,.5)` |
| Zone label | | Schibsted 13 / 600 `rgba(42,33,24,.5)`, first word of the name |
| Document coverage | transparent; hover `rgba(181,97,59,.06)`; selected `.07` | `#B3A894` 2.8 dashed 10 5 at .5; hover brand-dark solid opacity 1; selected brand 3.4 solid |
| Cadastral parcel | zone colour at .5 (`#4F6D82` .7 public, `#9E5568` .75 restitution) | `rgba(42,33,24,.20)` .6; hover ink 1.2; selected brand 2.2 + `drop-shadow(0 2px 6px rgba(181,97,59,.4))` |
| Urban parcel | transparent; hover `rgba(181,97,59,.14)`; selected `.22` | brand-dark 1.6 dashed 4 3; hover 2.6 solid; selected brand 2.8 solid + drop shadow |
| Land use | zone colour at .32 | none |
| FAR heat | `rgba(198,107,74, .15 + far/3.4 × .55)`, multiply | none |
| Price heat | band colour (§3), multiply | none |
| Planned traffic | | `#5b5b5b` 6 px dashed 10 6, .55 |
| Roads / river | river `#BCCEC8` .85 | roads white 9 / `#F2EBDC` 4.5 round caps; river line `#A4B9B2` 1 |
| Uncovered area | `#E6E9EE` | `#DCE1E8` 1 |
| Selection pin | brand, white 1.5 stroke, white 3.6 dot; 26 px tall, at the planned parcel's centre | |

**Panel anatomy.** `.pscroll` scrolls (8 px scrollbar, thumb `--paper-2`). Sticky `.phead`
(padding 20 22 18, hairline below): `.pclose` 28 × 28 at top 15 / right 16; `.peyebrow` mono
11.5 / 700 brand uppercase .16em with a `.tag` chip (brand tint, padding 2 9) followed by plain
text (the zone type); `.ptitle` 26 / 700; `.psub` mono 11.5 ink-2. `.backlink` (13 / 600
brand-dark, margin 12 22 −2). `.idgrid` (margin 16 22 4; 2 columns, 1 px `--line` gaps, hairline
border, cells white padding 10 13: key mono 11.5 .08em uppercase ink-2 .9, value mono 13 / 700;
`.full` spans both). Sections `.sect` padding 22, hairline between; `.secthead` (margin-bottom
11): label 20 / 700 + `.badge` (`free` brand tint / brand-dark; `paid` gold tint / gold with a lock
icon) and, right-aligned, the `.srcref` chip (mono 11.5 brand on brand tint, 999px, padding 1 7,
document icon + "source"; hover brand bg / white). Rows `.prow` padding 10 0 with a dashed
`--paper-2` divider: key 14 ink-2, value mono 13 / 700 right-aligned, unit `.u` 12 / 400 at .65.
`.vscard` (paper bg, hairline, padding 12, 66 × 52 mini diagram + 13 px ink-2 text, numbers mono
13.5 ink, delta mono 700 gold). `.upcard` (gold tint, `rgba(176,138,46,.3)` border, padding 13 14,
hover gold border + shadow; `.upn` mono 13 / 700 gold, −.02em; `.upd` 14 ink-2; `.upgo` 13.5 / 600 gold
with an arrow that slides 3 px on hover; `.none` variant paper / hairline, cursor default, `.upn`
ink-2). `.lockedlist` (hairline, white; rows padding 9 12, hairline between: name 14 / 500 ink with
a 13 px ink-2 description, right side a `.lockval` chip mono 10.5 / 700 .08em gold "LOCKED" +
`.lu` unit mono 11.5). `.lockcta` (gold tint, margin-top 11, padding 11 13: 32 px gold tile with a
lock, bold 15 + 13 ink-2 text, gold button mono 12.5 / 700 padding 9 14). `.roihero` (sits inside the section and keeps its own margin 6 22 0, so it is inset twice: 303 px wide in a
392 px panel; ink bg, paper text, padding 18 20: label mono 11.5 .16em uppercase .75, value 44 / 700, range
mono 13 .85, decorative sparkline bottom-right at .25). `.rangebar` 6 px track `--paper-2`, fill
brand .35 from 12 % to 88 %, 2 px brand-dark mark at the expected position, captions mono 9.5
ink-2 .7 (`low`, `expected`, `high`). `.assum` (paper, hairline, padding 12, margin-top 8: head
13 / 600 "◐ Test your own assumptions"; rows label 13 ink-2 + range input (accent brand) + value
mono 13 / 700 width 70 right). `.ctastack` (padding 20 22, gap 10, paper bg, hairline above):
`.cta` 50 px, radius 8, 15 / 600, left-aligned icon + label, trailing `small` mono 12.5 .8 pushed
right; `gold` (paid bg, white; hover `#6D28D9`), `ghost` (white, hairline, ink; hover ink border),
`line` (white, hairline, ink-2; hover brand border + text), `primary` (brand; hover brand-dark).
Inside modal footers the CTAs are 44 px, 13.5 px, auto width, centred text.

**Modals.** `.mhead` padding 22 26 18, hairline below: 42 px icon tile (radius 8; tinted per
modal: gold for order, brand for payment / engine / methodology, ink for upgrade), `.meyebrow` mono
13 / 700 .16em uppercase in the modal's colour, `h2` 26 / 700, `p` 14 ink-2 lh 1.5, close `✕`
30 × 30. `.mbody` padding 24 26, scrolls. `.mfoot` padding 18 26, paper bg, hairline above, buttons
right, `.fnote` mono 11.5 ink-2 .8 on the left (ellipsised). Forms: `.field` margin-bottom 14,
label 13 / 600 with an optional mono 11.5 `.opt` hint, inputs 46 px, hairline, radius 8, padding
0 13, 14 px, focus brand border; `.frow` two equal columns gap 12. `.ctx` context strip (paper,
hairline, padding 11 13, 32 px brand-tint icon tile, 13 px ink-2 text with mono 13.5 ink bold).
`.ordersum` (gold tint, `rgba(176,138,46,.3)` border; rows padding 10 14, 14 px, label + mono
gold value; `em` 12 ink-2 sub-label; `.deliv` row on `rgba(255,255,255,.5)` with brand-dark
value). `.fieldlab` + `.seg` segmented control (paper, hairline, radius 8; segments padding 11 12,
14 / 500 ink-2, hairline between; `.on` brand bg white 600). `.methlink` (white, hairline, padding
11 13, brand icon, bold 14 + 13 ink-2, mono `→` that slides on hover; hover brand border + tint).
`.paysummary` (paper, padding 14 16; lines 14 ink-2 padding 5 0; `.total` hairline above, 17 / 700
ink). `.paycard` (hairline, padding 13, brand-dark mono 13 / 700 brand name). `.securenote` mono 12.5
ink-2 centred. `.success` (centred, padding 14 10: 64 px brand-tint circle with a brand check,
`h2` 26, `p` 14 ink-2 max 360, `.orderref` mono 13 / 700 on paper with a dashed hairline, 999px,
padding 9 14). `.plans` three columns gap 12 (one column ≤ 860): `.plan` 1.5 px hairline, radius 8,
padding 16 15, hover brand border; `.feat` gold border with a gold 9 px "Unlocks this" tag
straddling the top edge; `h3` 18 / 700, `.pr` 34 / 700 with mono 13 `small`, `.pd` 13 ink-2 min
46, `li` 13 ink-2 with a 14 px brand check, CTA 38 px. Engine modal: `.ftable` (hairline, rows
grid `1.05fr 1.35fr .6fr` padding 9 13: name 12.5 / 600, formula mono 11.5 brand-dark, source
10.5 ink-2 right) and `.itable` (rows `.8fr 1.6fr`); `.growbox` brand tint, 12 px ink-2 with a
brand-dark bold lead. Methodology (wide modal): `.method` grid `272px 1fr`, min-height 432; left
`.mrail` paper with hairline right, heading mono 11.5 .16em "Six steps", steps padding 11 22 with a
24 px numbered circle (mono 11 / 600; `on` brand bg + white, `done` brand tint + brand-dark with
`✓`), 3 px brand bar on the active step; right `.mpane` padding 28 30: eyebrow mono 11.5 .16em
brand, `h2` 26, phase 13 ink-2, `.mdiagram` (paper, hairline, radius 8, min-height 150, centred
line drawing), `.mbodytext` 14 px lh 1.65 with `.sub` items (14.5 px) indented 20 px and a mono brand key
(`a`–`d`), footer `.mnav` with "Step n / 6", a 3 px progress bar (brand fill), `← Back`, `Next
step →` or, on the last step, gold `Order this analysis →`.

**AI panel** (mock only, see §9): head ink bg padding 13 15 with a 30 px brand avatar tile,
title 15 / 600, sub mono 13 .75; quota strip brand tint 12.5 brand-dark "n of 3 free interactions
left this session" with three 7 px dots (used = `--line`); body paper, padding 15, gap 12; bubbles
14 px lh 1.55 padding 11 13 radius 8, bot white with hairline, user brand / white, citations mono
11.5 brand with a document icon; suggestion chips 11.5 ink-2 999px (hover brand); footer input
42 px + 38 px brand send button.

**Admin console** (mock; the real staff tool reuses this chrome): `.adminbar` 60 px white with
hairline, title 20 / 700 with a plus-circle icon "Admin console", tabs 13.5 / 600 padding 8 13
radius 8 (active brand tint + brand-dark; hover paper), `← Back to map` ghost button right;
`.adminbody` padding 28 32 scrolls; `.astat-grid` four tiles gap 14 (white, hairline, radius 8,
shadow, padding 15 17: label 13 ink-2 500, value 44 / 700, mono 11.5 delta line, `.up` brand /
`.warn` gold); `.card` white hairline radius 8 shadow margin-bottom 20 with `.cardhd` padding 14
18 (h3 18 / 700 + 13 ink-2 sub) and `.tbl` (th mono 11.5 / 700 .1em uppercase ink-2 on paper,
padding 11 18; td 14 padding 11 18 with a `--paper-2` top hairline; row hover paper); status
phrases `.st`; `.abtn` 13 / 600 padding 7 13 brand (ghost: white hairline ink-2); review items
padding 14 18 with the extracted value as a brand-dark mono chip on brand tint and Approve /
Amend buttons.

**Breakpoints.** ≤ 1100: panel 360, rail 182 (cards and dep-notes 158), AI fab / panel at right
380. ≤ 860: rail hidden, panel becomes an absolute right-hand overlay (full width, max 400), AI
fab / panel at right 16 / bottom 16, search takes the full width, legend hidden, plans in one
column. ≤ 760: methodology modal drops its step rail. The POC is desktop-first (exclusions doc);
the breakpoints still ship because they are in the stylesheet.

## 6. Screens and states

Each row names the screenshot in `docs/wireframe/screens/` (1440 × 900 unless stated) and how
the state is reached. Copy is in §7.

| Screen | State | Screenshot | Trigger / content |
|---|---|---|---|
| S1 map landing | empty panel | `empty.png` | Load: rail, legend, map at city extent, panel with brand mark, "Pick a parcel to begin", hint chips; toast 0.9 s after load (`toast.png`) |
| S1 | pin dropped | `pin.png` | Click on land with no parcel: pin on the map, coordinates chip updated, empty panel gains the `.pinnote` |
| S2 search | suggestions | `search.png` | Focus or typing: dropdown of matches with `▤` zone, `⌂` address, `#` parcel reference, `⚠` outside coverage; "No match…" row when nothing matches |
| S2 | uncovered result | `uncovered.png` | Selecting an outside-coverage suggestion: selection cleared, panel hidden, coverage pill shown (mock: 2.6 s; real app: while the location is uncovered), uncovered area flashes |
| S3 zone | zone panel | `zone.png` | `selectZone`: eyebrow ZONE, documents list with status phrases, zone-level typical values, hint paragraph; no CTA stack |
| S3 document | planning-document panel | `doc.png` | Click on a coverage area: document details with `source` chip, general information, coverage counts + per-zone rows; CTAs "Ask about this document", "How we read a planning document" |
| S3 cadastral | with urban parcel | `cadastral.png` | Click on a cadastral parcel: id grid, gold "Corresponding urban parcel" card, cadastral→urban `vscard`; CTA stack |
| S3 cadastral | none defined | `cadastral-none.png` | Same with the "Not defined" card |
| S3 urban | locked market data | `urban.png`, `urban-scrolled.png` | Click on an urban parcel outline or the gold card: back-link, id grid, "Cadastral vs urban parcel", planning parameters (7 rows), locked market list + unlock card; CTA stack |
| S3 urban | market data visible | `urban-paid.png`, `urban-paid-scrolled.png` | After "subscribe": ROI hero, range rows, assumptions sliders, disclaimer |
| S4 order | individual | `order.png` | Gold CTA: context strip, order summary, segmented type, name / phone / e-mail fields, methodology link, guest note, footer |
| S4 order | legal entity | `order-legal.png` | Segment "Legal entity": company, PIB / VAT, contact, phone, e-mail, registered address; parcel over 500 m² shows €200 |
| S5 payment | card mock | `pay.png` | "Continue to payment →": summary, provider cards, card fields (mock; POC replaces this, §9) |
| S5 confirmation | success | `success.png` | "Order confirmed", reference chip, Done; toast "Order placed — check your email" |
| upgrade | plans | `upgrade.png`, `upgrade-ai.png` | Lock / paid layer / AI quota: three plan cards, the relevant one featured (mock only, §9) |
| engine | formulas modal | `engine.png` | "How the figures are calculated" (wide): formulas table, input data, growth note, disclaimer |
| methodology | step 2 / last step | `method.png`, `method-last.png` | Six-step wizard with rail, diagram, body, progress and navigation |
| AI | conversation / quota spent | `ai.png`, `ai-quota.png` | Fab → panel; quota dots; after the third question the chips become "↑ Upgrade for unlimited" (mock only, §9) |
| rail | collapsed | `rail-collapsed.png` | Chevron: 46 px rail with a single opener button |
| layers | every overlay on | `layers-all.png` | Ownership, restitution, land use, FAR heat, traffic and (subscribed) price heat on; full legend |
| layers | dependency | `dep.png` | Public ownership on while Cadastral parcels is off: dimmed card, `▲`, dep-note, legend suffix "— needs cadastral parcels" |
| legend | minimised | `legend-min.png` | `–` → `+`: header only |
| admin | Overview | `admin.png` | Four stat tiles + pipeline status table |
| admin | AI review queue / Planning rules / Financial assumptions / Calculation engine / Orders / Data sources | `admin-review.png`, `admin-rules.png`, `admin-fin.png`, `admin-engine.png`, `admin-orders.png`, `admin-data.png` | Tabs (mock content; the real screens are the staff tool's) |
| responsive | 1000 × 800 / 820 × 800 / 740 × 800 | `narrow-1100.png`, `narrow-860.png`, `narrow-760.png` | The three breakpoints with the urban panel open |

## 7. Copy (verbatim from the mock; English only, Montenegrin to be supplied)

- Topbar: placeholder `Search an address, click the map, or enter a parcel number…`; kbd `⌘K`;
  nav `Map`, `Admin`; pill `PODGORICA · PILOT` (title `Return to Podgorica`).
- Rail: `Map layers`; groups `Base` `Parcels` `Context` `Feasibility`; layer names in §5;
  `Subscription`; dep-note `Needs` **`Cadastral parcels`** `tap to turn on`; tooltips `<name>
  (core layer, always on)`, `<name> — included with the market-data subscription`, `<name> —
  shades cadastral parcels, so it only shows when “Cadastral parcels” is on`, `Toggle <name>`;
  collapse `Collapse` / `Show map layers`.
- Legend: `Legend`; group titles `Planning documents`, `Urban zones`, `Cadastral parcels`,
  `Urban parcels`, `Public ownership`, `Restitution / legal`, `Land use`, `FAR intensity` (unit
  `floor area ratio`), `Planned traffic`, `Price heatmap` (unit `€/m² land`); rows `Coverage area
  — click to open`, the five zone types, `Urban block boundary`, `Parcel outline`, `Parcel — click
  to open`, `Publicly owned`, `Legal claim` (+ ` — needs cadastral parcels`), `Low → high`,
  `Planned route`, price bands `not saleable`, `under €1,300`, `€1,300 – 1,700`, `€1,700 –
  2,100`, `€2,100 and above`; `No overlays active`.
- Map: `Outside current coverage` + `no adopted plan`; `250 m`; `42.4411° N · 19.2636° E`;
  `1.0×`; feature tooltips `<document> — click to open this planning document`, `Cadastral parcel
  #<n>[/<sub>][ → <UP>]`, `<UP> — urban parcel (building rights)`.
- Empty panel: `Pick a parcel to begin`; `Click a cadastral parcel, an urban parcel, or a plan
  coverage area. UrbanView reads the adopted plan and tells you what can be built — and whether
  it's worth building.`; chips `◆ click a parcel` `▨ click a plan area` `⇕ scroll to zoom` `✥ drag
  to pan` `⌕ search address`; pin note `Pin dropped at <coords> — no parcel at this point.`
- Zone panel: eyebrow `ZONE` + type; sub `Internal city division · ≈ city quarter`; `Planning
  documents` + `Free`; `In Montenegro a zone isn't an official bounded area — it's UrbanView's own
  grouping of related planning documents, roughly a city quarter. This zone groups <n>.`; doc
  rows `<name>` / `source PDF · eRegistri` / `Adopted` or `In progress`; `Zone-level planning`:
  `Predominant land use`, `Typical FAR (II)`, `Typical coverage (IZ)`, `Typical height`; `Click a
  specific parcel inside this zone to see its full planning parameters, the existing-vs-planned
  comparison, and the market feasibility.`
- Document panel: eyebrow `PLANNING DOCUMENT` + `adopted plan`; sub `DUP — Detailed urban plan`
  / `PUP — General urban plan` / `PGR — General regulation plan`; `Document details` + `Free` +
  `source`; rows `Document name`, `Type`, `Status`, `Source` (`PDF · eRegistri`), `Amendments`;
  `General planning information`: `This adopted plan governs building rights across its coverage
  area. Predominant land use is <use>. Per-parcel parameters — land use, height, coverage, FAR —
  are defined on the urban parcels inside it; click any parcel to read them.`; `Coverage`: `Urban
  zones spanned`, `Cadastral parcels`, `Urban parcels`, per-zone `<floors> · FAR <n>`; CTAs `Ask
  about this document`, `How we read a planning document`.
- Cadastral panel: eyebrow `CADASTRAL PARCEL` + type; title `Parcel #<n>[/<sub>]`; sub `<zone> ·
  <KO>`; grid `Parcel number`, `Cadastral municipality`, `Urban block`, `Cadastral area`,
  `Governing document`; `Corresponding urban parcel`; card `This cadastral parcel corresponds to
  an urban parcel in the adopted plan. Building rights — land use, height, coverage, FAR — are
  defined on the **urban parcel**, not on the cadastral one.` / `Open urban parcel →`; comparison
  `Cadastral <a> m² → urban <b> m². −<d>% taken for roads / public space.`; none: `Not defined` /
  `The adopted plan defines no urban parcel over this cadastral parcel, so building rights cannot
  be read directly. An expert analysis is needed to establish what is possible here.`
- Urban panel: eyebrow `URBAN PARCEL` + type; back-link `← cadastral parcel #<n>`; grid `Urban
  parcel`, `Cadastral parcel`, `Cadastral municipality`, `Urban block`, `Governing document`;
  `Cadastral vs urban parcel` + comparison + `All calculations use the urban parcel area.`;
  `Planning parameters` + `Free` + `source`: `Land use designation`, `Max building height`, `Max
  site coverage (IZ)` %, `Floor Area Ratio (II)`, `Planned parcel area` m², `Max Gross Floor
  Area` m² (brand-dark), `Max coverage area` m²; `Market data & feasibility` + `Subscription`;
  locked intro `These are the parameters UrbanView calculates for this urban parcel. The figures
  behind them are part of the market-data subscription.`; locked rows `Estimated land value`
  (`urban parcel area × zone land rate`, €), `Construction cost` (`gross floor area × build rate`,
  €), `Design & documentation` (`gross floor area × design rate`, €), `Estimated market value`
  (`saleable area × sale rate`, €), `Estimated saleable area` (`gross floor area × saleable
  share`, m²), `Potential profit` (`market value − total cost`, €), `Return on investment` (`profit
  ÷ total cost, as a range`, %); `LOCKED`; unlock card `Unlock the figures` / `Values, ranges and
  the assumptions sandbox.` / `Unlock →`; hero `Return on investment`, `<n>%`, `range <lo>% —
  <hi>% · expected <n>%`; range captions `expected`; assumptions `◐ Test your own assumptions`,
  `Construction €/m²`, `Sale price €/m²`, `Saleable %`; disclaimer `Figures are indicative ranges
  from Realitica, Estitor & Monstat — not investment advice. Deterministic calculation; AI does
  not generate financial values.`
- CTA stack: `Order expert analysis` + `€100` / `€200`; `Ask the AI assistant`; `How we analyze
  this parcel`.
- Order modal: eyebrow `Pay per service · one-off`; `Order expert analysis`; `A qualified expert
  produces a site analysis & feasibility study for this parcel — interpretation, hidden risks,
  development scenarios and benchmarking beyond the automated figures. Delivered by email.`;
  context `Analysing <Parcel #n · KO> — carried through automatically, no need to re-enter.`;
  summary `Parcel size` / `urban parcel` or `cadastral parcel`, `Analysis fee` / `up to 500 m²`
  or `over 500 m²`, `Expected delivery` / `2–5 working days`; note `Prototype pricing is set by
  parcel size alone — €100 up to 500 m², €200 above. Later phases can also weigh the
  planning-document area and other factors affecting the complexity of the analysis.`; `Ordering
  as` `Individual` / `Legal entity`; fields `First name` (`Marko`), `Last name` (`Petrović`),
  `Telephone` (`+382 …`), `Email address` (`you@email.me`); legal: `Company name` (`Company
  d.o.o.`), `PIB / VAT number` (`02345678`), `Contact person`, `Telephone`, `Email address`
  (`office@company.me`), `Registered address` + `for the invoice` (`Bulevar Svetog Petra
  Cetinjskog 1, Podgorica`); link `How we analyze this parcel` / `The six steps this report
  follows, from locating the parcel to the final package.`; `No account needed — guest checkout.
  You'll get the report and an order reference by email.`; footer `€<price> · 2–5 working days`,
  `Cancel`, `Continue to payment →`.
- Payment modal (mock): `Secure checkout`, `Payment`, `One-off payment in EUR. Card details never
  touch UrbanView.`, lines `Expert site & feasibility analysis`, `Parcel #<n> · <area> m²`,
  `Ordering as`, `Expected delivery`, `Total due`; `Stripe` `Card · Apple Pay · Google Pay`
  `selected`; `Paddle` `Merchant of record · handles VAT`; `Card number`, `Expiry`, `CVC`;
  `PCI-compliant · provider verification pending (Stripe vs Paddle)`; `Pay €<price>`.
- Success: `Order confirmed`; `An expert will prepare your site & feasibility analysis and email
  it within 2–5 working days. A confirmation is on its way now.`; `<UV-XXXXX> · Parcel #<n>`;
  `Done`.
- Upgrade modal (mock): `Three ways to go deeper`, `Choose your access`, `Planning parameters
  and exploring the city are always free. These unlock the value layers.`; plans `Per-report`
  `€100–200 /site` … `Order a report`; `Market data` `€29 /mo` … `Subscribe`; `AI unlimited` `€19
  /mo` … `Subscribe`; `Unlocks this`; footnote `Subscriptions require a free account. Three
  revenue channels are being tested in the pilot to see which delivers the most value — this is a
  mockup, no charge.`
- Engine modal: `Engine v1.4 · updated August 2026`; `How the figures are calculated`; `Every
  value comes from UrbanView's own formulas applied to the parameters read from the adopted plan.
  Nothing is generated by a language model.`; `Formulas`, `Input data`; `The engine gets more
  precise over time.` + paragraph; `Indicative ranges, not investment advice. Deterministic
  calculation — the AI assistant reads these figures but never generates them.`; footer `Engine
  v1.4 · 9 formulas · 5 input sources`, `Close`. The formula rows in the mock are illustrative
  (one says land value × 0.55): the real modal lists the client-owned formulas of
  `formula_version poc-1` from the engine, never the mock's.
- Methodology: `How we work`, `Our analysis methodology`, `The step-by-step process we follow
  every time we analyze a parcel — for our services, or under contract to design the object.
  Context: <location>.`; `Six steps`; steps 1 `Locate the parcel` (Research, `Phase 1 · Research &
  ownership`), 2 `Extract planning parameters` (Research, `Phase 1 · Read the adopted plan`,
  items a–d), 3 `2D orthogonal projection` (Design, `Phase 2 · Design`), 4 `3D fitting` (Design,
  `Phase 2 · Massing in context`), 5 `Preliminary package & feasibility` (Deliverable, `Phase 3 ·
  Client package`), 6 `Offer & design` (Engagement, `Phase 4 · Engagement`); bodies verbatim in
  `wireframe.js` (`METHOD_STEPS`); `Step n / 6`, `← Back`, `Next step →`, `Order this analysis →`.
- AI (mock): `UrbanView AI`, `Custom-trained · cites its sources`, `<n> of 3 free interactions
  left this session`, chips `What can I build here?` `Explain FAR and coverage` `Why is the planned
  parcel smaller?` `Is this parcel worth developing?`, placeholder `Ask about this location…`,
  `↑ Upgrade for unlimited`.
- Toasts: `Click any parcel to see what can be built`; `Core layer — always visible`; `<layer>
  shades cadastral parcels — turn on “Cadastral parcels” to see it`; `Cadastral parcels turned
  on`; `Centred on Podgorica`; `Order placed — check your email`; `Market data unlocked`; `AI
  assistant unlocked — unlimited`.
- Admin (mock): tabs `Overview`, `AI review queue`, `Planning rules`, `Financial assumptions`,
  `Calculation engine`, `Orders`, `Data sources`; `← Back to map`; tiles `Parcels ingested`,
  `Planning documents`, `Pending AI review`, `Paid orders`; card titles and column headers as in
  `wireframe.js`.

## 8. Interactions

- **Selection.** Cadastral parcel click → cadastral panel; urban parcel outline click (or the
  gold card / back-link) → urban panel; coverage-area click → document panel; click on land with
  no parcel → pin + empty panel with coordinates; `✕` clears the selection and the pin. A drag
  never selects (3 px threshold). The selected feature is re-drawn on top with the `sel` style
  and the pin sits at the planned parcel's centre. Selecting a parcel or dropping a pin updates the coordinates chip.
- **Map.** Wheel zooms at the cursor (`exp(−deltaY × 0.0018)`), `+` / `−` zoom by 1.4 at the
  centre, reset returns to 1.0× and clears the selection, the topbar pill re-centres with a toast;
  bounds 0.4×–6×; drag pans only when zoomed in (mock). With Mapbox: same controls, same copy, city
  extent on load.
- **Search.** Focus or typing opens suggestions (filter on title and subtitle); click selects and
  fills the input; click outside closes; `Ctrl/⌘ K` focuses; `Esc` closes suggestions and any
  modal. In the real app: `GET /v1/geocode` for addresses, `GET /v1/locate/parcel` for a parcel
  number (needs the KO), `GET /v1/locate` after picking a suggestion.
- **Layers.** Toggle re-renders rail, map and legend; core layers only toast; turning on a
  dependent layer whose requirement is off toasts and shows the dep-note (click = turn the
  requirement on); the paid layer opens the upgrade flow (POC: §9). Every toggle emits
  `layer_toggled`.
- **Legend** minimises to its header. **Rail** collapses to 46 px.
- **Panel.** Sticky header, internal scroll, sticky CTA stack at the bottom. Assumption sliders
  recalculate on `input` (live), the real app through the shared engine in the browser and
  `POST /v1/feasibility` for the authoritative answer. `source` chips open the cited page
  (`viewer_url`, emit `source_reference_opened`).
- **Order flow.** Gold CTA → order modal (segment switch re-renders with the type kept) →
  `Continue to payment →` → (mock: card modal → success). Cancel and `✕` close; clicking the
  overlay closes. Success toasts.
- **Modals** trap focus; the methodology rail is clickable; `Esc` closes.
- **Intro toast** 0.9 s after load.

## 9. POC deviations (the only ones allowed)

| Mock | POC build | Source |
|---|---|---|
| Hand-drawn SVG city, random parcels | Mapbox GL JS + the published PMTiles layers (`GET /v1/tiles/current`), styled with the values in §5 | Real data |
| `ZONES` / `DOCS` / `PARCELS` mock data, 7 planning rows | `GET /v1/locate`, `GET /v1/panel`; the planning section renders **all 13 `planning.fields`** in dictionary order with the same `.prow` component (`not_stated` values render `—`), every stated value with its `source` chip | Product rule: every value cites its source; the field dictionary is the contract |
| Market data locked behind a subscription, `LOCKED` chips, upgrade modal, price-heatmap lock | Group 2 ticket (2026-09-28) restores the mock: `LOCKED` chips by default, "Unlock →" and the locked price heatmap open **"Choose your access"** as designed; "Subscribe" records the intent (`market_data_interest` / `ai_interest`, with the parcel ids) and, for market data, unlocks for the session (no checkout, no charge); the AI plan is noted only (the assistant is a shell). Unlocked: ROI hero, range rows, a plain design & documentation row that still shows its range, the wireframe's assumption sliders (values marked yours / default, "Reset to defaults", inline error for an out-of-bounds edit), the engine strip (empty in the final mock) opening the engine modal, and the intent "I want market data updates" | Group 2 ticket; technical scope §1.2 / §3.4 |
| AI assistant fab, chat panel, quota, upgrade | The fab ("3 free") and the 380×544 panel ship as a **UI shell** (setup ticket, 2026-09-24): greeting, quota strip, chips and input as in the mock; a question or chip records `ai_interest` and toasts that the assistant is not live. The ghost CTA becomes the intent button **"Ask about this site"** | Setup ticket; technical scope §1.2; exclusions doc (AI assistant) |
| Card payment modal (Stripe / Paddle) | S4 / S5 ticket: no card fields anywhere and no payment step, so the order form's gold button reads **"Place order →"** (the mock's "Continue to payment →") and creates the order (`POST /v1/orders`); the confirmation is the mock's success layout followed by "Pay by bank transfer": the API's instructions as `.paysummary` lines (payee, IBAN, bank / SWIFT when configured, payment reference, "Amount due" as the total line) with "Copy" on IBAN and reference, the API's note, where they were emailed and a link to the order page. The form adds inline validation messages and a one-sentence failure note (the mock has no validation state). Which of the three documented flows applies is open (§10) |
| No order page after the confirmation | `/orders/<reference>`: the public order page the confirmation and every order email link to (status, location, turnaround from `GET /v1/orders/{reference}/status`; no personal data, no login), built from the mock's parts (topbar logo, a card in the modal's style, `.orderref`, `.paysummary` steps with the dot + word status labels, `.ordersum` rows) | S4 / S5 ticket | Build plan v2 (bank transfer, implemented in the API); exclusions doc (order form only); technical scope (hosted checkout) |
| Engine modal content (v1.4, 9 mock formulas, land value × 0.55) | Formulas and inputs of the shared engine (`formula_version`, `client_validated`), disclaimer from the API | Formulas are client-owned |
| Admin console overlay with mock tabs | The wireframe's overlay ships inside the frontend (setup ticket): admin bar, the seven tabs, "← Back to map"; each tab shows a placeholder card until its screen is built (technical scope A1 documents, A2 review queue, A4 publish, A5 assumptions, A6 orders, A7 analytics and audit, as far as they are in scope) | Setup ticket; technical scope §8.2 |
| English only | Setup ticket: a language button in the topbar nav (mono `ME` / `EN`, the language it switches to, kept at every width) re-renders every shell string (topbar, search, rail, legend, map chrome, empty panel, assistant shell, toasts, disclaimer) from one string table, no reload; English (the mock's copy) stays the default (`NEXT_PUBLIC_DEFAULT_LANG`) and the Montenegrin strings are drafts; bilingual API text picks `_en` / `_me`. Panels and modals are still English (§10) | Setup ticket; Product |
| Search: a fixed list of mock rows filtered on title and subtitle | S2 ticket: the same rows and icons from real sources (zones `GET /v1/zones`, addresses `GET /v1/geocode`, sub-label `Address · <zone>`, ⚠ for hits outside coverage); a parcel number shows one `Parcel #…` row plus a **KO picker** (chips in the rail's field-selector style) because the KO is mandatory; an unknown reference is an inline neutral row with every KO offered again; **recent searches** (last 5, this browser) when the input is focused and empty; at ≤ 760 px the search is a **full-screen sheet** with `Cancel` | S2 ticket (2026-09-24); `frontend/CLAUDE.md` "Search" |
| Cadastral and urban parcel panels (Group 1): mock parcels, one source chip per section | S3 parcel ticket (2026-09-28): the mock's markup from `GET /v1/panel`; a source icon after every stated planning value (one click to its cited page); the mock's seven planning rows, then the dictionary's other stated fields (building line, setbacks, parking, green area, utilities); max building height as `27.5 m · P+8`; planned parcel area from the plan's text, else the geometry; a split cadastral parcel shows one urban-parcel card per planned parcel and compares against their total; a planned parcel as large or larger is worded as such; "No adopted plan" card for an uncovered parcel; a Parcel ID meta line on the urban panel; the gold order CTA shows the configured price and, until the order form exists, a toast | S3 parcel ticket; `frontend/CLAUDE.md` "Information panel (S3)" |
| Zone and document panels: fixed mock documents and zone values | S3 ticket (2026-09-28): the same markup from `GET /v1/panel`; document meta adds the adoption date when known and `n parcels with data` / `not yet digitised`; a covered document in a zone's list opens its document panel; a zone without an adopted plan shows a "No adopted plan" note; a superseded status chip (neutral); typical height as `24 m · 7 floors` (the data has metres and a floor count, not the `P+5+Pk` notation); a source chip on "Zone-level planning"; text-valued rows wrap when too long (the mock's `.pv` is `nowrap`); the methodology's gold "Order this analysis →" says to pick a parcel until the order flow exists | S3 ticket; `frontend/CLAUDE.md` "Information panel (S3)" |
| `source` chips are labels (title "Traceable to source document"), nothing opens | Source viewer ticket: every source chip, row source icon and stored document in a zone's list opens the **source viewer** in the wireframe's wide modal (`.modal.wide`, 90vh; full screen at ≤ 760 px): header in the modal's style, the cited PDF page rendered in the app (PDF.js), the value's bbox as a translucent brand rectangle, page ‹ › + page input, zoom − / +, "Open PDF ↗", a page-shaped loading shimmer and a neutral "This page could not be loaded." + Retry | Product rule: every value is traceable to its document in one click; `frontend/CLAUDE.md` "Source viewer" |
| Tablet / phone layouts (≤ 860 px: rail hidden, panel as a right-hand overlay) | Desktop-first; at ≤ 860 px the rail collapses into a drawer and the panel becomes a **bottom sheet** (peek / half / full, handle cycles them) with the chrome above it; ≤ 760 px icon-only topbar (`frontend/src/styles/overrides.css`) | Setup ticket ("functional on tablet and phone") |

Everything else (tokens, sizes, spacing, copy, icons, animations, hover and focus states, order of
elements) is reproduced as the mock has it.

**Scope is not decided here.** The exclusions document (2026-09-24, POC 265 h) leaves out the
outside-coverage behaviour (S6), front-end event instrumentation, tablet and phone layouts, the
admin review / publish / orders / analytics screens and transactional e-mail; the build plan v2 and
the backend already built include several of them. Whatever is built follows this spec; what is
built is the product owner's call (§10, item 7).

## 10. Open items for the product owner

1. The `#6D28D9` purple hover on gold buttons (§3): reproduce, or replace with `--paid-dark`?
2. Montenegrin copy: the switch and the string table are built (setup ticket), with draft
   Montenegrin strings and English as the default. The ticket wants Montenegrin as the default:
   approve the copy (`frontend/src/lib/i18n/strings.ts`), then set
   `NEXT_PUBLIC_DEFAULT_LANG=me`. Panels and modals still need their strings moved into the table.
3. Wording of the two intent buttons (proposed: "Unlock full market data", "Ask about this
   site", as in the technical scope) and their toasts.
4. Order price tiers (`ORDER_PRICE_TIERS`) and the turnaround shown in the order summary.
5. Resolved by the setup ticket: the AI fab and panel stay as a UI shell that records
   `ai_interest`. Wording of its "not live yet" toast is provisional.
6. Placement of the dictionary fields the mock does not show (building line, setbacks, parking,
   minimum green area, utilities): confirm they belong in the free planning section.
7. Scope conflict: build plan v2 (220 h: review queue, publish job, bank transfer, e-mail) vs the
   exclusions document (265 h POC without them) vs the technical scope (688 h pilot with hosted
   checkout). Which one governs the frontend and admin build?
8. Layers without data in the pilot: planned traffic (MVP per the technical scope) and ownership /
   restitution (only with confirmed bulk cadastral access). Recommendation: leave them out of the
   rail and the legend until a published layer has features, rather than show an empty toggle.
   The mock also folds urban blocks into "Urban zones" while the technical scope lists blocks as
   their own layer; this spec keeps the mock.
9. At ≤ 860 px the mock floats the map tools and the AI fab over its overlay panel
   (`screens/narrow-860.png`). Superseded by the bottom sheet, which keeps them above it.
10. Search (S2): the second S2 text asks for two modes (Address / Parcel number) and the events
    `search_address`, `search_parcel`, `search_no_result`. Built as the wireframe's single input
    (a typed parcel number switches to the parcel row + KO picker) and as
    `search_performed {search_kind, matched, result}`, the API's enum. Confirm, or ask for tabs
    and new event names (a backend enum change).
11. Zone panel (S3): the second S3 text asks for a breadcrumb "Podgorica › {zone}", a
    "Zoom to zone" button and "close returns to the map with the zone still highlighted"; the
    wireframe has none of them and its ✕ clears the selection. Not built; the search already
    frames the zone. Confirm, or place them. Its status wording "in force" is built as the
    wireframe's Adopted / In progress / Superseded.
12. English plan type names: the wireframe says `PUP — General urban plan`; the profile's
    Montenegrin entry glosses PUP as "spatial-urban plan". The map shows the wireframe's words
    (`terminology.document_types_en`); confirm the English names with the client.
13. `panel_viewed` is sent as `{panel_type, zone_id | document_id}` (the API's typed keys) where
    the ticket wrote `{type, id}`.
14. Parcel panel ghost CTA: the S3 parcel ticket and the mock say "Ask the AI assistant"; §9's AI
    row (setup ticket) renames it "Ask about this site". Built as the ticket says; pick one.
15. Dictionary fields beyond the mock's seven planning rows (building line, setbacks, parking,
    green area, utilities) are listed after them (item 6 is still open): confirm, or hide them.
16. Group 2: the second Group 2 text lists GFA and coverage area among the feasibility fields;
    they stay in Group 1 as in the mock. Its intent buttons are built as "I want market data
    updates" (unlocked section) and the CTA stack's "Ask the AI assistant" (`ai_interest` with the
    parcel ids on click, where the first text says "when the AI quota is exhausted": the pilot's
    assistant answers nothing, so every question is interest). The disclaimer shows the
    wireframe's wording until the lawyer approves the API's (`disclaimer_status`).
17. Assumption sandbox: the second sandbox text asks whether the sale price is one value that
    shifts low / expected / high proportionally or three inputs ("agree with client"). Built as
    the wireframe's single slider; the engine keeps each rate's bounds around the edited value.
    The edits are kept per tab (`sessionStorage`), not in the URL.
18. Source viewer: the second text asks for a side panel on desktop; built as the wireframe's
    wide modal (its only overlay pattern; the map stays visible behind the dimmed overlay, as the
    first text says), full screen on phones, radius 8 px (effective) where the ticket says 18.
    "Open the full document" is decided as an "Open PDF ↗" button (the whole PDF at the cited
    page in a new tab); confirm. The admin review queue reuses the component once that screen is
    built. The "FAR highlighted on 10 values" check ran on the sample's placeholder PDFs (each
    value printed at its bbox); real documents depend on the extraction's bboxes. Checked in
    Chromium at phone width, not yet on a real iPhone (Safari).
19. Orders (S4 / S5): the second text asks for pages (`/order/new?parcel=`, `/order/{reference}`)
    with "name, email, company (optional)", a terms / disclaimer checkbox, the visitor's edited
    assumptions shown read-only, and an `order_submitted` event. Built as the first text and the
    wireframe: modals over the map with the mock's two forms (telephone required, as the API
    requires it), no checkbox (the mock has none and the disclaimer wording still awaits the
    lawyer: confirm whether ordering needs an explicit acceptance), the edited assumptions sent
    with the order and stored in its snapshot but not shown (the mock has no row for them),
    `checkout_completed` when the API creates the order (`order_submitted` is not in the API's
    enum), and a public order page at `/orders/<reference>` (status only, not the payment
    instructions again). `checkout_completed` carries the reference as `order_id` (the API's typed
    key, which the analytics dashboard counts orders by) where the first text wrote
    `{order_reference}`.
20. Turnaround: the mock shows "2–5 working days"; the API has one configured number
    (`ORDER_TURNAROUND_BUSINESS_DAYS` = 5, counted from the payment), shown as "5 working days". A
    range needs a minimum in configuration; confirm the wording with the client. The order page's
    status lines and step names are provisional copy.
21. Setup ticket (second text): the admin as a separate app with shared design tokens is the
    reserved `admin/` item (the wireframe's admin overlay stays in the map app until then);
    `/order/...` routes are one redirect to the order page (S4–S5 stay modals, item 19); the layer
    card's "legend slot" is the wireframe's single legend card, and its "not covered" note is
    "no data yet" when the published version has nothing for the layer; the session id header is
    the analytics session (new after 30 minutes idle), random hex rather than a UUID.
