import { CoachThread } from "@/components/coach/coach-thread";

// PageHeader is rendered INSIDE CoachThread (not here) so it can sit in the
// same sticky wrapper as the action-pill bar — the whole top region (title
// + subtitle + Make Plan / Review Health / Memory / End & Save) stays
// pinned together as the conversation scrolls beneath it.
export default function CoachPage() {
  return (
    <div className="mx-auto w-full max-w-4xl">
      <div className="px-5 pb-8 sm:px-8">
        <CoachThread />
      </div>
    </div>
  );
}
