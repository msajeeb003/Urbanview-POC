// Bundles the PMTiles tile provider (src/lib/map/pmtiles-provider.ts + the pmtiles library) into
// one self-contained ES module that Mapbox GL's workers import: public/map/urbanview-pmtiles.js.
// Runs before `next dev` and `next build` (predev / prebuild); the output is generated, not committed.
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const outfile = resolve(here, "../public/map/urbanview-pmtiles.js");
mkdirSync(dirname(outfile), { recursive: true });

await build({
  entryPoints: [resolve(here, "../src/lib/map/pmtiles-provider.ts")],
  outfile,
  bundle: true,
  format: "esm",
  platform: "browser",
  target: ["es2020"],
  minify: true,
  legalComments: "none",
  logLevel: "warning",
});
console.log(`map tile provider -> ${outfile}`);
