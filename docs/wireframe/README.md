# Wireframe: "UrbanView — Urban feasibility engine"

The interactive mockup the client approved. **The frontend reproduces it exactly** (decision of
2026-09-24); the design contract that says how is `docs/specs/frontend-design.md`.

| File | Contents |
|---|---|
| `UrbanView New Design.html` | The mockup as delivered (self-unpacking bundle; needs JavaScript). Re-shared on 2026-09-24 as "UrbanView New Design (1).html", byte-identical (sha256 `b089cb46…`). |
| `wireframe-decoded.html` | The unpacked page (fonts do not resolve; opens with fallback fonts). |
| `wireframe.css` | The app stylesheet, verbatim. Port it unchanged as the global stylesheet. |
| `wireframe.js` | The app script, verbatim: mock data, every template, copy string and interaction. |
| `screens/` | One PNG per app state (1440×900; three narrow widths), rendered with the real fonts, the brand SVGs and a seeded cadastre. Acceptance references. |
| `computed-styles.json` | `getComputedStyle` of 257 selectors across the states + the resolved `:root` tokens: the effective values after the stylesheet's override passes. |
| `make_screens.py` | Regenerates `screens/` and `computed-styles.json` with headless Chrome / Edge: `python docs/wireframe/make_screens.py [state…] [--dump]`. |

Brand assets extracted from the bundle are in `docs/brand/`.
