import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  devIndicators: false,
  // Emit a self-contained server (.next/standalone) for the production Docker
  // image (Stage 9, Package 3).
  output: "standalone",
};

export default nextConfig;
