import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      exclude: ["src/types.ts"], // type declarations only: no runtime code to cover
      reporter: ["text", "json-summary"],
      // The formulas are the product: every line, branch and function of the engine is exercised.
      thresholds: { lines: 100, functions: 100, branches: 100, statements: 100 },
    },
  },
});
