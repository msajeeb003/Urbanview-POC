// Copies PDF.js's worker (the legacy build: older mobile Safari too) next to the app so the source
// viewer can point GlobalWorkerOptions.workerSrc at /pdfjs/pdf.worker.min.mjs. PDF.js itself is
// imported lazily by the viewer, so neither file touches the map's first load. Runs before
// `next dev` and `next build` (predev / prebuild); the copy is generated, not committed.
import { copyFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const source = resolve(dirname(require.resolve("pdfjs-dist/package.json")), "legacy/build/pdf.worker.min.mjs");
const target = resolve(here, "../public/pdfjs/pdf.worker.min.mjs");
mkdirSync(dirname(target), { recursive: true });
copyFileSync(source, target);
console.log(`pdf.js worker -> ${target}`);
