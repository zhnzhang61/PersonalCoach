import { Card, CardContent } from "@/components/ui/card";
import type { LucideIcon } from "lucide-react";

interface Props {
  title: string;
  description: string;
  Icon: LucideIcon;
}

export function PlaceholderCard({ title, description, Icon }: Props) {
  return (
    <Card className="border-dashed">
      <CardContent className="flex items-start gap-3 p-4">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-md bg-muted/40">
          <Icon className="size-5 text-muted-foreground" aria-hidden />
        </div>
        <div className="space-y-1">
          <p className="text-sm font-semibold">{title}</p>
          <p className="text-xs text-muted-foreground">{description}</p>
          <p className="eyebrow text-[10px]">Coming soon</p>
        </div>
      </CardContent>
    </Card>
  );
}
