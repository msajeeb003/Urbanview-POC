/**
 * Version identifiers carried by every result.
 *
 * - `FORMULA_VERSION` changes only when the client changes a formula or the range derivation
 *   (the fixtures are versioned with it); the assumptions editor shows it next to every number.
 * - `ENGINE_VERSION` is the package version: it changes with any code release, formulas or not.
 */
export const FORMULA_VERSION = "poc-1" as const;
export const ENGINE_VERSION = "1.0.0" as const;

/** How low / expected / high are built from the market inputs (see `deriveRanges` in engine.ts). */
export const RANGE_DERIVATION = "pessimistic-pairing-v1" as const;
