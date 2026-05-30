import { PageHeader } from "@/components/page-header";
import { TodayEyebrow } from "@/components/today-eyebrow";
import { BlocksSection } from "@/components/setup/blocks-section";
import { SyncSection } from "@/components/setup/sync-section";

export default function SetupPage() {
  return (
    <div className="mx-auto w-full max-w-3xl">
      <PageHeader eyebrow={<TodayEyebrow />} title="Setup" />
      <div className="space-y-6 px-5 pb-8 sm:px-8">
        <SyncSection />
        <BlocksSection />
      </div>
    </div>
  );
}
