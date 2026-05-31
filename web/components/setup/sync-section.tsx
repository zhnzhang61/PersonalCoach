"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { formatDistanceToNow, parseISO } from "date-fns";
import { ClipboardPaste, ExternalLink, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet, apiPost } from "@/lib/api";
import { GARMIN_SSO_URL, chromeDeepLink } from "@/lib/sso";
import { cn } from "@/lib/utils";
import type {
  RefreshTokenResult,
  SyncResult,
  SyncStatus,
} from "@/lib/types";

export function SyncSection() {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["sync", "garmin", "status"],
    queryFn: () => apiGet<SyncStatus>("/api/sync/garmin/status"),
    refetchInterval: 30_000,
  });

  const sync = useMutation({
    mutationFn: () => apiPost<SyncResult>("/api/sync/garmin"),
    onSettled: () => qc.invalidateQueries({ queryKey: ["sync", "garmin", "status"] }),
  });

  const lastSyncRel = status.data?.last_sync
    ? formatDistanceToNow(parseISO(status.data.last_sync), { addSuffix: true })
    : null;

  const showRefreshFlow =
    status.data?.outcome === "token_expired" ||
    sync.data?.reason === "token_expired";

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-4 p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="eyebrow">Garmin sync</div>
              <div className="font-heading mt-1 text-2xl font-semibold tracking-tight sm:text-3xl">
                {status.isLoading ? (
                  <Skeleton className="h-7 w-40" />
                ) : lastSyncRel ? (
                  `Updated ${lastSyncRel}`
                ) : (
                  "No data yet"
                )}
              </div>
              {status.data?.outcome === "ok" && (
                <p className="mt-1 text-sm text-emerald-700 dark:text-emerald-400">
                  Last attempt succeeded.
                </p>
              )}
              {status.data?.outcome === "error" && (
                <p className="mt-1 text-sm text-rose-700 dark:text-rose-400">
                  Last attempt failed (not a token issue).
                </p>
              )}
            </div>
            <Button
              type="button"
              size="lg"
              onClick={() => sync.mutate()}
              disabled={sync.isPending}
              className="shrink-0"
            >
              <RefreshCw
                className={cn("size-4", sync.isPending && "animate-spin")}
                aria-hidden
              />
              {sync.isPending ? "Syncing…" : "Sync now"}
            </Button>
          </div>

          {sync.data?.ok && (
            <p className="text-sm text-emerald-700 dark:text-emerald-400">
              Sync complete.
            </p>
          )}
          {sync.data?.reason === "error" && (
            <p className="text-sm text-rose-700 dark:text-rose-400">
              Sync failed: {sync.data.stderr?.trim() || "see server logs"}
            </p>
          )}
          {sync.error && (
            <p className="text-sm text-rose-700 dark:text-rose-400">
              Could not reach API: {(sync.error as Error).message}
            </p>
          )}
        </CardContent>
      </Card>

      {showRefreshFlow && <RefreshTokenCard />}
    </div>
  );
}

function RefreshTokenCard() {
  const qc = useQueryClient();
  const [ticket, setTicket] = useState("");
  const [pasteError, setPasteError] = useState<string | null>(null);

  const refresh = useMutation({
    mutationFn: (t: string) =>
      apiPost<RefreshTokenResult>("/api/sync/garmin/refresh-token", {
        ticket: t,
      }),
    onSuccess: (data) => {
      if (data.ok) {
        setTicket("");
        qc.invalidateQueries({ queryKey: ["sync", "garmin", "status"] });
      }
    },
  });

  // One-tap path: read whatever was copied from Chrome's address bar
  // (the full redirect URL, or a bare ST-…-sso) straight off the
  // clipboard and submit it. The backend's parse_service_ticket accepts
  // either form, so we don't validate the shape here — we only guard the
  // two cases the user can actually hit: clipboard unreadable, or empty.
  // Everything else (wrong/expired ticket) surfaces through the existing
  // refresh error UI below, same as the manual path.
  //
  // navigator.clipboard.readText() needs the tap as its user gesture
  // (it has it) and, on iOS, pops the system "Allow Paste" confirmation
  // the first time per copy — that extra tap is unavoidable (privacy)
  // but still far less fiddly than select-in-field + paste + submit.
  const pasteAndRefresh = async () => {
    setPasteError(null);
    let text = "";
    try {
      text = await navigator.clipboard.readText();
    } catch {
      setPasteError(
        "读不到剪贴板（可能没授权）。请在下面的输入框手动粘贴。",
      );
      return;
    }
    const trimmed = text.trim();
    if (!trimmed) {
      setPasteError(
        "剪贴板是空的——先在 Chrome 地址栏整条 URL 上 Copy，再回来点这里。",
      );
      return;
    }
    setTicket(trimmed); // show what we're submitting; stays for retry/edit
    refresh.mutate(trimmed);
  };

  return (
    <Card className="border-warm-accent/40 bg-warm-bg/40">
      <CardContent className="space-y-4 p-5 sm:p-6">
        <div>
          <div className="eyebrow">Token expired</div>
          <h3 className="font-heading mt-1 text-xl font-semibold tracking-tight sm:text-2xl">
            Refresh Garmin login
          </h3>
          <p className="mt-2 text-sm text-muted-foreground">
            Garmin tokens expire every day or two. Tap below, sign in to
            Garmin, then copy the ticket from the redirect URL and paste it
            back here.
          </p>
        </div>

        <ol className="space-y-3 text-sm">
          <li className="flex items-start gap-3">
            <span className="font-heading mt-0.5 text-base font-semibold text-foreground">
              1.
            </span>
            <div className="flex-1 space-y-2">
              <p>Open the Garmin sign-in page in Chrome.</p>
              {/* base-ui Button doesn't support asChild — style an anchor
                  directly with buttonVariants so it looks identical
                  while still being a real <a>.

                  href uses Chrome's iOS deep-link scheme (chromeDeepLink)
                  instead of the plain https URL: this app is a standalone
                  PWA, and iOS opens external links from a standalone PWA
                  in an in-app Safari sheet regardless of the user's
                  default browser. The googlechromes:// scheme forces the
                  real Chrome app, whose address bar the next step needs.
                  No target=_blank — the link launches another app, so a
                  new tab would just be left blank. */}
              <a
                href={chromeDeepLink(GARMIN_SSO_URL)}
                className={cn(
                  buttonVariants({ variant: "secondary" }),
                  "w-full sm:w-auto",
                )}
              >
                <ExternalLink className="size-4" aria-hidden />
                Open Garmin sign-in
              </a>
            </div>
          </li>
          <li className="flex items-start gap-3">
            <span className="font-heading mt-0.5 text-base font-semibold text-foreground">
              2.
            </span>
            <p className="flex-1">
              Sign in. The page will redirect to a &ldquo;Site can&rsquo;t be
              reached&rdquo; error — that&rsquo;s expected. Copy the entire
              URL from the address bar (or just the{" "}
              <code className="rounded bg-muted px-1 py-0.5 text-xs">
                ST-...-sso
              </code>{" "}
              portion).
            </p>
          </li>
          <li className="flex items-start gap-3">
            <span className="font-heading mt-0.5 text-base font-semibold text-foreground">
              3.
            </span>
            <div className="flex-1 space-y-2">
              <p>
                Back here within ~1 minute (the ticket expires fast), tap{" "}
                <strong>Paste &amp; refresh</strong> — it reads the URL you
                just copied straight from the clipboard.
              </p>
              <Button
                type="button"
                onClick={pasteAndRefresh}
                disabled={refresh.isPending}
                className="w-full sm:w-auto"
              >
                <ClipboardPaste className="size-4" aria-hidden />
                {refresh.isPending ? "Exchanging…" : "Paste & refresh"}
              </Button>
              {pasteError && (
                <p className="text-sm text-rose-700 dark:text-rose-400">
                  {pasteError}
                </p>
              )}
              <p className="pt-1 text-xs text-muted-foreground">
                Or paste it manually:
              </p>
              <Input
                value={ticket}
                onChange={(e) => setTicket(e.target.value)}
                placeholder="https://...?ticket=ST-... or ST-...-sso"
                className="font-mono text-xs"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
              />
              <Button
                type="button"
                variant="secondary"
                onClick={() => refresh.mutate(ticket.trim())}
                disabled={!ticket.trim() || refresh.isPending}
              >
                {refresh.isPending ? "Exchanging…" : "Submit ticket"}
              </Button>
            </div>
          </li>
        </ol>

        {refresh.data?.ok && (
          <p className="text-sm text-emerald-700 dark:text-emerald-400">
            ✓ Token refreshed. You can hit Sync now to pull fresh data.
          </p>
        )}
        {refresh.data && !refresh.data.ok && (
          <p className="text-sm text-rose-700 dark:text-rose-400">
            Exchange failed: {refresh.data.stderr?.trim() || "unknown error"}
          </p>
        )}
        {refresh.error && (
          <p className="text-sm text-rose-700 dark:text-rose-400">
            Could not reach API: {(refresh.error as Error).message}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
