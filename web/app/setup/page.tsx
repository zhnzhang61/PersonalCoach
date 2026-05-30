import { format } from "date-fns";
import { PageHeader } from "@/components/page-header";
import { BlocksSection } from "@/components/setup/blocks-section";
import { SyncSection } from "@/components/setup/sync-section";

export default function SetupPage() {
  const today = format(new Date(), "EEEE, MMMM d");
  return (
    <div className="mx-auto w-full max-w-3xl">
      <PageHeader eyebrow={today} title="Setup" />
      <div className="space-y-6 px-5 pb-8 sm:px-8">
        <SyncSection />
        <BlocksSection />
      </div>
    </div>
  );
}
