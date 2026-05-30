// `sticky` (default true) wraps the header in a `sticky top-0` container
// with a bottom border + blurred background, so the title region stays
// pinned at viewport top as the page content scrolls beneath it. Pass
// `sticky={false}` when an ancestor already provides the sticky wrapper
// (e.g. CoachThread, which pins the title together with its action-pill
// row in a single sticky block).
export function PageHeader({
  eyebrow,
  title,
  subtitle,
  sticky = true,
}: {
  eyebrow?: string;
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
    <div className="sticky top-0 z-30 border-b border-border bg-background/95 backdrop-blur-md">
      {headerEl}
    </div>
  );
}
