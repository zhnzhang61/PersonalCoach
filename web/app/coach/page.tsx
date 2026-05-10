import { PageHeader } from "@/components/page-header";
import { CoachThread } from "@/components/coach/coach-thread";

export default function CoachPage() {
  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader
        title="Coach"
        subtitle="Talk through training, health, and your week. The coach remembers what matters."
      />
      <div className="px-5 pb-8 sm:px-8">
        <CoachThread />
      </div>
    </div>
  );
}
