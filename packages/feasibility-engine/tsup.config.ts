import { defineConfig } from "tsup";

// Dual build: ESM (dist/index.js) for the Next.js apps, CJS (dist/index.cjs) for tooling that
// still requires it, plus a single .d.ts. No runtime dependencies are bundled because there are none.
export default defineConfig({
  entry: ["src/index.ts"],
  format: ["esm", "cjs"],
  outExtension: ({ format }) => ({ js: format === "cjs" ? ".cjs" : ".js" }),
  dts: true,
  sourcemap: true,
  clean: true,
  target: "es2022",
  treeshake: true,
  minify: false,
});
