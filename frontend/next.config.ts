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
  // S4–S5 live as modals over the map; the order page is /orders/<reference> (the e-mails' link).
  // /order/<reference> (the setup ticket's spelling) leads there too.
  async redirects() {
    return [{ source: "/order/:reference", destination: "/orders/:reference", permanent: false }];
  },
};

export default nextConfig;
