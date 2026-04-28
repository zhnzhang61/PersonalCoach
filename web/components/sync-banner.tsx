"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertCircle } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { apiGet } from "@/lib/api";
import type { SyncStatus } from "@/lib/types";

export function SyncBanner() {
  const pathname = usePathname();
  const status = useQuery({
    queryKey: ["sync", "garmin", "status"],
    queryFn: () => apiGet<SyncStatus>("/api/sync/garmin/status"),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });

  if (status.data?.outcome !== "token_expired") return null;
  // No need to nag the user if they're already on Setup, where the
  // refresh-token UI lives.
  if (pathname.startsWith("/setup")) return null;

  return (
    <Link
      href="/setup"
      className="block bg-rose-600/95 text-white shadow-sm transition-colors hover:bg-rose-700"
    >
      <div className="mx-auto flex max-w-4xl items-center gap-3 px-5 py-3 text-sm font-medium sm:px-8">
        <AlertCircle className="size-4 shrink-0" aria-hidden />
        <span className="flex-1">
          Garmin token expired — tap to refresh
        </span>
        <span className="shrink-0 text-xs font-semibold uppercase tracking-wider opacity-90">
          Setup →
        </span>
      </div>
    </Link>
  );
}
