"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, BarChart3, Heart, Settings } from "lucide-react";
import { cn } from "@/lib/utils";

const items = [
  { href: "/", label: "Health", icon: Heart, match: (p: string) => p === "/" },
  {
    href: "/activity",
    label: "Activity",
    icon: Activity,
    match: (p: string) => p.startsWith("/activity"),
  },
  {
    href: "/training",
    label: "Training",
    icon: BarChart3,
    match: (p: string) => p.startsWith("/training"),
  },
  {
    href: "/setup",
    label: "Setup",
    icon: Settings,
    match: (p: string) => p.startsWith("/setup"),
  },
];

export function BottomNav() {
  const pathname = usePathname();
  return (
    <nav
      className="fixed bottom-0 inset-x-0 z-40 border-t border-border bg-background/95 backdrop-blur-md"
      style={{ paddingBottom: "max(env(safe-area-inset-bottom), 4px)" }}
      aria-label="Primary"
    >
      <ul className="mx-auto flex max-w-2xl items-stretch justify-around">
        {items.map((item) => {
          const active = item.match(pathname);
          const Icon = item.icon;
          return (
            <li key={item.href} className="flex-1">
              <Link
                href={item.href}
                className={cn(
                  "flex flex-col items-center justify-center gap-0.5 pt-2 pb-1.5 text-[10px] font-medium tracking-wide transition-colors",
                  active
                    ? "text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
                aria-current={active ? "page" : undefined}
              >
                <Icon
                  className={cn(
                    "size-[22px]",
                    active ? "stroke-[2.25]" : "stroke-[1.75]",
                  )}
                  aria-hidden
                />
                <span>{item.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
