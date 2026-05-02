import * as fs from "node:fs";
import * as path from "node:path";
import type { NextConfig } from "next";

// The Python backend reads the monorepo-root .env directly. Next.js only
// auto-loads .env from its own app dir, so NEXT_PUBLIC_* keys (e.g. the
// Mapbox token) live there too without this — pull them in so we keep one
// source of truth for shared secrets.
const rootEnv = path.resolve(process.cwd(), "../.env");
if (fs.existsSync(rootEnv)) {
  for (const line of fs.readFileSync(rootEnv, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*(NEXT_PUBLIC_[A-Z0-9_]+)\s*=\s*(.*)$/);
    if (m && process.env[m[1]] === undefined) {
      process.env[m[1]] = m[2].trim().replace(/^['"]|['"]$/g, "");
    }
  }
}

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
