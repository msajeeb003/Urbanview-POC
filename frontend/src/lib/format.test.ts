import { describe, expect, it } from "vitest";

import { formatCoords, formatZoomFactor, parseParcelNumber, scaleBar } from "./format";

describe("formatCoords", () => {
  it("renders the wireframe chip format", () => {
    expect(formatCoords({ lat: 42.4411, lng: 19.2636 })).toBe("42.4411° N · 19.2636° E");
    expect(formatCoords({ lat: -1.5, lng: -70.25 })).toBe("1.5000° S · 70.2500° W");
  });
});

describe("formatZoomFactor", () => {
  it("is relative to the municipality framing", () => {
    expect(formatZoomFactor(12, 12)).toBe("1.0×");
    expect(formatZoomFactor(13, 12)).toBe("2.0×");
    expect(formatZoomFactor(11, 12)).toBe("0.5×");
  });
});

describe("scaleBar", () => {
  it("picks a nice distance that fits in 80 px", () => {
    const s = scaleBar(42.44, 15);
    expect(s.widthPx).toBeLessThanOrEqual(80);
    expect(s.widthPx).toBeGreaterThan(30);
    expect(s.label).toMatch(/^(1|2|2\.5|5)0*\s?m$|^\d+(\.\d)? km$/);
  });
  it("switches to km when zoomed out", () => {
    expect(scaleBar(42.44, 10).label).toMatch(/km$/);
  });
});

describe("parseParcelNumber", () => {
  it.each([
    ["1042", { number: "1042", sub: null }],
    ["1042/3", { number: "1042", sub: "3" }],
    [" #1042 / 3 ", { number: "1042", sub: "3" }],
    ["parcel 77", { number: "77", sub: null }],
  ])("%s", (input, expected) => {
    expect(parseParcelNumber(input)).toEqual(expected);
  });
  it.each(["Bulevar Save Kovačevića 12", "", "12a", "1042/"])("rejects %s", (input) => {
    expect(parseParcelNumber(input)).toBeNull();
  });
});
