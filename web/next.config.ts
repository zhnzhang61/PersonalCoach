import type { NextConfig } from "next";

const API_TARGET = process.env.API_BASE ?? "http://127.0.0.1:8765";

const nextConfig: NextConfig = {
  // Hosts allowed to load Next.js dev resources (HMR, /_next/* chunks). Without
  // this, requests from the Mac's Tailscale name/IP are blocked by Next 16's
  // dev-origin guard, leaving the iPhone client stuck on skeletons because
  // dev-only assets never finish loading. Wildcards cover whatever ts.net
  // hostname the Tailscale account ends up assigning.
  allowedDevOrigins: [
    "zhans-macbook-pro",
    "*.ts.net",
    "100.110.119.107",
  ],
  // Hide the floating "N" route-info badge in dev — we view this on the
  // iPhone where the badge crowds the bottom-left corner.
  devIndicators: false,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_TARGET}/api/:path*` },
    ];
  },
};

export default nextConfig;
