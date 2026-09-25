/**
 * The wireframe's inline SVG icons, copied path for path from `docs/wireframe/wireframe.js`
 * (sizes, strokes and colours included). They are part of the design: do not swap in an icon set.
 */
import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement>;

/** Topbar search magnifier (15×15). */
export const IconSearch = (p: P) => (
  <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden {...p}>
    <circle cx="6.5" cy="6.5" r="4.5" stroke="rgba(244,236,216,.55)" strokeWidth="1.5" />
    <path d="M10 10l3.5 3.5" stroke="rgba(244,236,216,.55)" strokeWidth="1.5" />
  </svg>
);

/** Topbar "Map" nav. */
export const IconMap = (p: P) => (
  <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden {...p}>
    <path d="M2 4l4-2 3 2 4-2v9l-4 2-3-2-4 2z" stroke="currentColor" strokeWidth="1.3" />
  </svg>
);

/** Topbar "Admin" nav (circle with a cross). */
export const IconAdmin = (p: P) => (
  <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden {...p}>
    <path d="M7.5 2v11M2 7.5h11" stroke="currentColor" strokeWidth="1.3" />
    <circle cx="7.5" cy="7.5" r="5.5" stroke="currentColor" strokeWidth="1.3" />
  </svg>
);

/** Admin bar title icon (17×17). */
export const IconAdminTitle = (p: P) => (
  <svg width="17" height="17" viewBox="0 0 17 17" fill="none" aria-hidden {...p}>
    <circle cx="8.5" cy="8.5" r="6.5" stroke="#12211f" strokeWidth="1.4" />
    <path d="M8.5 3v11M3 8.5h11" stroke="#12211f" strokeWidth="1.4" />
  </svg>
);

/** Rail collapse chevron. */
export const IconChevronLeft = (p: P) => (
  <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden {...p}>
    <path d="M9.5 3L5 7.5l4.5 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/** Rail opener (three bars, 16×16). */
export const IconMenu = (p: P) => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden {...p}>
    <path d="M2 3h12M2 8h12M2 13h12" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
  </svg>
);

/** White tick inside the layer card's check circle (8×8). */
export const IconCheckTiny = (p: P) => (
  <svg width="8" height="8" viewBox="0 0 8 8" aria-hidden {...p}>
    <path d="M1 4l2 2 4-5" stroke="#fff" strokeWidth="1.5" fill="none" />
  </svg>
);

/** Padlock used on paid layers and the Subscription badge (9×9 in a 12 box). */
export const IconLockSmall = ({ width = 9, height = 9, ...p }: P) => (
  <svg width={width} height={height} viewBox="0 0 12 12" fill="none" aria-hidden {...p}>
    <rect x="2" y="5.4" width="8" height="5.4" rx="1.2" fill="currentColor" />
    <path d="M4 5.4V4a2 2 0 014 0v1.4" stroke="currentColor" strokeWidth="1.3" />
  </svg>
);

/** Larger padlock for the unlock card (16×16 in an 18 box). */
export const IconLock = (p: P) => (
  <svg width="16" height="16" viewBox="0 0 18 18" fill="none" aria-hidden {...p}>
    <rect x="3" y="8" width="12" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
    <path d="M6 8V5a3 3 0 016 0v3" stroke="currentColor" strokeWidth="1.5" />
  </svg>
);

/** Document glyph of the "source" chip (10×11). */
export const IconDocSmall = ({ width = 10, height = 11, ...p }: P) => (
  <svg width={width} height={height} viewBox="0 0 10 11" fill="none" aria-hidden {...p}>
    <path d="M1 1h5l3 3v6H1z" stroke="currentColor" strokeWidth="1" strokeLinejoin="round" />
    <path d="M6 1v3h3" stroke="currentColor" strokeWidth="1" />
  </svg>
);

/** Document glyph of the zone panel's document list (15×15). */
export const IconDoc = (p: P) => (
  <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden {...p}>
    <path d="M3 1h6l3 3v10H3z" stroke="currentColor" strokeWidth="1.2" />
    <path d="M9 1v3h3" stroke="currentColor" strokeWidth="1.2" />
  </svg>
);

/** Outside-coverage warning triangle (14×14, white). */
export const IconWarn = (p: P) => (
  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden {...p}>
    <path d="M7 1l6 11H1z" stroke="#FFFFFF" strokeWidth="1.3" />
    <path d="M7 5v3M7 10h.01" stroke="#FFFFFF" strokeWidth="1.3" />
  </svg>
);

/** Map tools: reset / re-centre crosshair. */
export const IconReset = (p: P) => (
  <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden {...p}>
    <path d="M7.5 2.5v10M2.5 7.5h10" stroke="currentColor" strokeWidth="1.3" />
    <circle cx="7.5" cy="7.5" r="2" stroke="currentColor" strokeWidth="1.3" />
  </svg>
);

/** AI floating button speech bubble (22×22). */
export const IconChatFab = (p: P) => (
  <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden {...p}>
    <path d="M4 5h14v9H9l-4 3v-3H4z" stroke="#FFFFFF" strokeWidth="1.5" strokeLinejoin="round" />
    <circle cx="8" cy="9.5" r="1" fill="rgba(255,255,255,.14)" />
    <circle cx="11" cy="9.5" r="1" fill="#f4efe6" />
    <circle cx="14" cy="9.5" r="1" fill="#f4efe6" />
  </svg>
);

/** AI panel header avatar (17×17). */
export const IconChatAvatar = (p: P) => (
  <svg width="17" height="17" viewBox="0 0 17 17" fill="none" aria-hidden {...p}>
    <path d="M2 3h13v8H7l-3 2.5V11H2z" stroke="#fff" strokeWidth="1.3" strokeLinejoin="round" />
    <circle cx="6" cy="7" r="1" fill="rgba(255,255,255,.14)" />
    <circle cx="9" cy="7" r="1" fill="#fff" />
    <circle cx="12" cy="7" r="1" fill="#fff" />
  </svg>
);

/** AI panel send arrow (16×16). */
export const IconSend = (p: P) => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden {...p}>
    <path d="M2 8l12-5-5 12-2-5z" stroke="#fff" strokeWidth="1.3" strokeLinejoin="round" />
  </svg>
);

/** CTA "Order expert analysis" card icon (16×16, white). */
export const IconOrder = (p: P) => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden {...p}>
    <path d="M2 3h12v10H2z" stroke="#fff" strokeWidth="1.3" />
    <path d="M2 6h12M5 9h3" stroke="#fff" strokeWidth="1.3" />
  </svg>
);

/** CTA "Ask …" speech bubble (15×15). */
export const IconAsk = (p: P) => (
  <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden {...p}>
    <path d="M2 3h11v7H6l-3 2.5V10H2z" stroke="#12211f" strokeWidth="1.2" strokeLinejoin="round" />
  </svg>
);

/** CTA "How we analyze …" steps glyph (15×15). */
export const IconSteps = (p: P) => (
  <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden {...p}>
    <circle cx="3" cy="3" r="1.6" stroke="currentColor" strokeWidth="1.2" />
    <circle cx="3" cy="12" r="1.6" stroke="currentColor" strokeWidth="1.2" />
    <path d="M3 4.6v5.8M6.5 3H13M6.5 7.5H13M6.5 12H13" stroke="currentColor" strokeWidth="1.2" />
  </svg>
);

/** Empty-panel pin note glyph (13×13). */
export const IconPinSmall = (p: P) => (
  <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden {...p}>
    <path d="M6.5 1C4.6 1 3 2.6 3 4.5C3 7 6.5 12 6.5 12S10 7 10 4.5C10 2.6 8.4 1 6.5 1z" fill="#B5613B" stroke="#fff" strokeWidth="1" />
    <circle cx="6.5" cy="4.5" r="1.3" fill="#fff" />
  </svg>
);

/** Map selection pin, the wireframe's path at the mock's on-screen scale; anchor at the bottom. */
export const IconMapPin = (p: P) => (
  <svg width="30" height="39" viewBox="-13 -27 26 34" aria-hidden {...p}>
    <path d="M0,-26 c-7,0 -12,5 -12,12 c0,9 12,20 12,20 c0,0 12,-11 12,-20 c0,-7 -5,-12 -12,-12 z" fill="#B5613B" stroke="#ffffff" strokeWidth="1.5" />
    <circle cx="0" cy="-14" r="3.6" fill="#ffffff" />
  </svg>
);
