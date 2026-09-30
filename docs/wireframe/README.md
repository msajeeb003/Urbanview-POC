# Wireframe: "UrbanView — Urban feasibility engine"

The interactive mockup the client approved. **The frontend reproduces it exactly** (decision of
2026-09-24); the design contract that says how is `docs/specs/frontend-design.md`.

| File | Contents |
|---|---|
| `UrbanView New Design.html` | The mockup as delivered (self-unpacking bundle; needs JavaScript). Re-shared on 2026-09-24 as "UrbanView New Design (1).html", byte-identical (sha256 `b089cb46…`). |
| `wireframe-decoded.html` | The unpacked page (fonts do not resolve; opens with fallback fonts). |
| `wireframe.css` | The app stylesheet the POC ships (byte-identical to `frontend/src/styles/wireframe.css`): the mockup's, minus the rules of the components the POC plan does not fund, removed on 2026-10-01 (AI assistant fab / chat / quota, subscription plan cards, locked and paid states, Free / Paid badges, card payment, admin KPI tiles, the phase strip, layer dependency notes, and the 860 px / 760 px tablet and phone layouts). `wireframe-decoded.html` keeps the full original. |
| `wireframe.js` | The app script, verbatim: mock data, every template, copy string and interaction. |
| `screens/` | One PNG per app state the POC builds (1440×900, and the 1100 px width), rendered with the real fonts, the brand SVGs and a seeded cadastre. Acceptance references for the structure; the mock's own extra cards and chrome (ownership, restitution and traffic cards, the subscription lock, the AI fab) are not built. `urban.png` / `urban-scrolled.png` show Group 2 unlocked, as the POC shows it to everyone. |
| `computed-styles.json` | `getComputedStyle` of 202 selectors across the states + the resolved `:root` tokens: the effective values after the stylesheet's override passes (the removed components' selectors are left out). |
| `make_screens.py` | Regenerates `screens/` and `computed-styles.json` with headless Chrome / Edge: `python docs/wireframe/make_screens.py [state…] [--dump]`. |

Brand assets extracted from the bundle are in `docs/brand/`.
