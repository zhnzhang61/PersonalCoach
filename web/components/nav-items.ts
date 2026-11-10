import { Activity, BarChart3, Heart, MessageCircle, Settings } from "lucide-react";

// The five destinations, shared by the phone's bottom bar and the
// desktop sidebar so the two can never drift apart.
export const NAV_ITEMS = [
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
    href: "/coach",
    label: "Coach",
    icon: MessageCircle,
    match: (p: string) => p.startsWith("/coach"),
  },
  {
    href: "/setup",
    label: "Setup",
    icon: Settings,
    match: (p: string) => p.startsWith("/setup"),
  },
] as const;
