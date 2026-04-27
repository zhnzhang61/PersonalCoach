import { PageHeader } from "@/components/page-header";

export default function TrainingPage() {
  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader title="Training" subtitle="Coming soon" />
      <div className="px-5 pb-8 sm:px-8">
        <p className="text-sm text-muted-foreground">
          Run history, lap detail, and AI run analysis will live here. For now
          use the existing Streamlit dashboard for these views.
        </p>
      </div>
    </div>
  );
}
