"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV_ITEMS } from "@/components/nav-items";
import { cn } from "@/lib/utils";

// Desktop navigation (≥ lg). A phone-style bottom bar wastes a wide
// screen's height and puts the targets far from the content; a fixed
// left rail keeps them in one place and gives the pages the full height.
// Hidden below lg, where BottomNav takes over. Width is --side-nav-w
// (globals.css) so the layout's left padding always matches.
export function SideNav() {
  const pathname = usePathname();
  return (
    <nav
      aria-label="Primary"
      className="fixed inset-y-0 left-0 z-40 hidden w-[var(--side-nav-w)] flex-col border-r border-border bg-background lg:flex"
    >
      <div className="px-6 pt-8 pb-6">
        <div className="font-heading text-xl font-semibold tracking-tight">
          PersonalCoach
        </div>
      </div>
      <ul className="flex-1 space-y-1 px-3">
        {NAV_ITEMS.map((item) => {
          const active = item.match(pathname);
          const Icon = item.icon;
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  active
                    ? "bg-muted text-foreground"
                    : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                )}
              >
                <Icon
                  className={cn("size-[18px]", active ? "stroke-[2.25]" : "stroke-[1.75]")}
                  aria-hidden
                />
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
