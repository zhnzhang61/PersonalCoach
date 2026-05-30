import type { ReactNode } from "react";

// `sticky` (default true) wraps the header in a `sticky top-0` container
// with a bottom border + blurred background, so the title region stays
// pinned at viewport top as the page content scrolls beneath it. The
// `mb-5` on the sticky wrapper gives the page content ~one line-height of
// breathing room below the border line — without it the first card
// (e.g. "Today's Check-in") visually crashes into the underline.
//
// Pass `sticky={false}` when an ancestor already provides the sticky
// wrapper (e.g. CoachThread, which pins the title together with its
// action-pill row in a single sticky block).
//
// `eyebrow` is `ReactNode` (not just `string`) so callers can pass a
// client component like <TodayEyebrow /> — that's how Server Component
// pages get a dynamic date without baking the build-time date into the
// SSG HTML (see PR #99 review).
export function PageHeader({
  eyebrow,
  title,
  subtitle,
  sticky = true,
}: {
  eyebrow?: ReactNode;
  title: string;
  subtitle?: string;
  sticky?: boolean;
}) {
  const headerEl = (
    <header className="px-5 pt-8 pb-5 sm:px-8 sm:pt-12">
      {eyebrow && <div className="eyebrow mb-2">{eyebrow}</div>}
      <h1 className="font-heading text-4xl font-semibold leading-[1.05] tracking-tight sm:text-5xl">
        {title}
      </h1>
      {subtitle && (
        <p className="mt-2 max-w-prose text-sm text-muted-foreground sm:text-base">
          {subtitle}
        </p>
      )}
    </header>
  );
  if (!sticky) return headerEl;
  return (
    <div className="sticky top-0 z-30 mb-5 border-b border-border bg-background/95 backdrop-blur-md">
      {headerEl}
    </div>
  );
}
