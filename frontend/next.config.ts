import path from "node:path";

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // npm workspace: dependencies are hoisted to the repository root.
  turbopack: {
    root: path.join(__dirname, ".."),
  },
  // The server image (frontend/Dockerfile) builds a self-contained server (`NEXT_OUTPUT=standalone`,
  // traced from the repository root because of the workspace); local builds stay as they are.
  ...(process.env.NEXT_OUTPUT === "standalone"
    ? { output: "standalone" as const, outputFileTracingRoot: path.join(__dirname, "..") }
    : {}),
};

export default nextConfig;
