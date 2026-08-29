"use client";

import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";

// Touch-only pull-to-refresh. Wraps page content; when the page is
// scrolled to the top and the user drags down past the threshold,
// `onRefresh` runs and the indicator spins until it settles.
//
// Native touch listeners (passive: false) rather than React handlers —
// preventDefault inside a passive listener is ignored, and without it
// iOS Safari's own rubber-banding swallows the gesture. Desktop mouse
// users are untouched: the Sync button still exists for them.

const THRESHOLD = 70;   // px of (dampened) pull that arms a refresh
const HOLD = 52;        // px the indicator holds at while refreshing

export function PullToRefresh({
  onRefresh,
  children,
}: {
  onRefresh: () => Promise<unknown>;
  children: React.ReactNode;
}) {
  const [pull, setPull] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const startY = useRef<number | null>(null);
  const pullRef = useRef(0);
  const busyRef = useRef(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;

    const onStart = (e: TouchEvent) => {
      if (busyRef.current || window.scrollY > 0) return;
      startY.current = e.touches[0].clientY;
    };

    const onMove = (e: TouchEvent) => {
      if (startY.current == null || busyRef.current) return;
      const delta = e.touches[0].clientY - startY.current;
      if (delta <= 0 || window.scrollY > 0) {
        pullRef.current = 0;
        setPull(0);
        return;
      }
      // Dampen so a long drag reads as resistance, not a page teleport.
      e.preventDefault();
      const eased = Math.min(120, delta * 0.45);
      pullRef.current = eased;
      setPull(eased);
    };

    const onEnd = () => {
      const armed = pullRef.current >= THRESHOLD;
      startY.current = null;
      if (!armed) {
        pullRef.current = 0;
        setPull(0);
        return;
      }
      busyRef.current = true;
      setRefreshing(true);
      setPull(HOLD);
      void Promise.resolve()
        .then(onRefresh)
        .finally(() => {
          busyRef.current = false;
          setRefreshing(false);
          pullRef.current = 0;
          setPull(0);
        });
    };

    el.addEventListener("touchstart", onStart, { passive: true });
    el.addEventListener("touchmove", onMove, { passive: false });
    el.addEventListener("touchend", onEnd, { passive: true });
    el.addEventListener("touchcancel", onEnd, { passive: true });
    return () => {
      el.removeEventListener("touchstart", onStart);
      el.removeEventListener("touchmove", onMove);
      el.removeEventListener("touchend", onEnd);
      el.removeEventListener("touchcancel", onEnd);
    };
  }, [onRefresh]);

  const armed = pull >= THRESHOLD;

  return (
    <div ref={wrapRef} style={{ overscrollBehaviorY: "contain" }}>
      <div
        aria-hidden={pull === 0}
        className="pointer-events-none flex items-end justify-center overflow-hidden transition-[height] duration-150"
        style={{ height: pull }}
      >
        <div
          className={cn(
            "mb-3 flex size-9 items-center justify-center rounded-full border border-border bg-background shadow-sm",
            armed && !refreshing && "border-warm-accent",
          )}
        >
          <RefreshCw
            className={cn(
              "size-4 text-muted-foreground",
              armed && "text-warm-accent",
              refreshing && "animate-spin",
            )}
            style={
              refreshing ? undefined : { transform: `rotate(${pull * 2.2}deg)` }
            }
            aria-hidden
          />
        </div>
      </div>
      {children}
    </div>
  );
}
