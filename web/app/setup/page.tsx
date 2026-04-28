import { PageHeader } from "@/components/page-header";
import { SyncSection } from "@/components/setup/sync-section";

export default function SetupPage() {
  return (
    <div className="mx-auto w-full max-w-3xl">
      <PageHeader title="Setup" />
      <div className="space-y-6 px-5 pb-8 sm:px-8">
        <SyncSection />
      </div>
    </div>
  );
}
