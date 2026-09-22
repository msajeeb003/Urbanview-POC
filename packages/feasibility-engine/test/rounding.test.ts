import { describe, expect, it } from "vitest";

import { DECIMALS, roundHalfAwayFromZero } from "../src/index.js";

describe("roundHalfAwayFromZero (shared with the Python engine)", () => {
  it("rounds on the decimal representation, not the binary one", () => {
    expect(roundHalfAwayFromZero(1.005, 2)).toBe(1.01); // (1.005).toFixed(2) would give "1.00"
    expect(roundHalfAwayFromZero(2.675, 2)).toBe(2.68);
    expect(roundHalfAwayFromZero(-1.005, 2)).toBe(-1.01); // away from zero
    expect(roundHalfAwayFromZero(0.125, 2)).toBe(0.13);
    expect(roundHalfAwayFromZero(2817.4999999999995, 2)).toBe(2817.5);
  });

  it("leaves values with fewer decimals untouched", () => {
    expect(roundHalfAwayFromZero(1295460, 2)).toBe(1295460);
    expect(roundHalfAwayFromZero(527.78, 2)).toBe(527.78);
    expect(roundHalfAwayFromZero(3.2, 0)).toBe(3);
    expect(roundHalfAwayFromZero(2.5, 0)).toBe(3);
  });

  it("handles exponent notation and tiny or huge magnitudes", () => {
    expect(roundHalfAwayFromZero(1.5e-7, 2)).toBe(0);
    expect(roundHalfAwayFromZero(0.005, 2)).toBe(0.01);
    expect(roundHalfAwayFromZero(0.004, 2)).toBe(0);
    expect(roundHalfAwayFromZero(1.5e21, 2)).toBe(1.5e21);
    expect(roundHalfAwayFromZero(123456789.129, 2)).toBe(123456789.13);
  });

  it("normalises negative zero", () => {
    expect(Object.is(roundHalfAwayFromZero(-0, 2), 0)).toBe(true);
    expect(Object.is(roundHalfAwayFromZero(-0.001, 2), 0)).toBe(true);
  });

  it("rejects non-finite values", () => {
    expect(() => roundHalfAwayFromZero(Number.NaN, 2)).toThrow(RangeError);
    expect(() => roundHalfAwayFromZero(Number.POSITIVE_INFINITY, 2)).toThrow(RangeError);
  });

  it("uses two decimals for every unit", () => {
    expect(DECIMALS).toEqual({ m2: 2, EUR: 2, "%": 2 });
  });
});
