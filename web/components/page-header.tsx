export function PageHeader({
  eyebrow,
  title,
  subtitle,
}: {
  eyebrow?: string;
  title: string;
  subtitle?: string;
}) {
  return (
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
}
