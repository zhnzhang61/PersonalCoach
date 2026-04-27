import { PageHeader } from "@/components/page-header";

export default function SetupPage() {
  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader title="Setup" subtitle="Coming soon" />
      <div className="px-5 pb-8 sm:px-8">
        <p className="text-sm text-muted-foreground">
          Garmin sync, AI telemetry settings, and training block management will
          live here. For now use the existing Streamlit dashboard.
        </p>
      </div>
    </div>
  );
}
