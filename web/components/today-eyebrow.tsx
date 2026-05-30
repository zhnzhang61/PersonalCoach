"use client";

// Tiny client component that renders today's date ("Friday, May 29") in
// the user's local timezone. Used as the `eyebrow` on every tab header.
//
// Why a client component instead of `format(new Date())` inline in a
// Server Component (the previous pattern on Health / Activity, extended
// to the other tabs in this PR before the review caught it):
//
//   1. Static generation (Next.js App Router default for a page with no
//      dynamic API) would render `new Date()` ONCE at build time and bake
//      that date into the HTML. Users would see "Wednesday, May 28"
//      forever until the next deploy.
//   2. Even in dynamic-per-request mode, `new Date()` evaluates in the
//      SERVER's tz, not the user's — symmetrical to the PERSONAL_COACH_TZ
//      issue PR #84 fixed for the agent prompt.
//
// Empty initial render + useEffect → no SSG-baked date in the HTML
// payload at all, and the value the user sees is computed in their
// browser tz. Trade-off: ~one paint of empty eyebrow before hydration.
// Tolerable for a label of this prominence.

import { useEffect, useState } from "react";
import { format } from "date-fns";

export function TodayEyebrow() {
  const [date, setDate] = useState<string>("");
  // The empty-state → useEffect → setState pattern is intentional here:
  // SSR/SSG renders "" so no date gets baked into the HTML; the effect
  // runs only after hydration, when `new Date()` reflects the user's
  // browser clock + tz. eslint's `set-state-in-effect` (React 19+ rule)
  // is right to flag setState-in-effect for *derived* state, but this is
  // a deliberate hydration deferral — there's no equivalent value to
  // compute during render that wouldn't also bake into SSR HTML.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setDate(format(new Date(), "EEEE, MMMM d"));
  }, []);
  return <>{date}</>;
}
