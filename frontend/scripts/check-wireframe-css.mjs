// Fails when src/styles/wireframe.css is no longer a byte-for-byte copy of the approved
// wireframe stylesheet (docs/wireframe/wireframe.css). Changes go to src/styles/overrides.css.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const approved = resolve(here, "../../docs/wireframe/wireframe.css");
const shipped = resolve(here, "../src/styles/wireframe.css");
const sha = (p) => createHash("sha256").update(readFileSync(p)).digest("hex");

const a = sha(approved);
const b = sha(shipped);
if (a !== b) {
  console.error(
    `wireframe.css differs from the approved stylesheet\n  approved ${a}  ${approved}\n  shipped  ${b}  ${shipped}\n` +
      "Put design changes in src/styles/overrides.css; never edit wireframe.css.",
  );
  process.exit(1);
}
console.log(`wireframe.css matches the approved stylesheet (sha256 ${a.slice(0, 12)}…)`);
