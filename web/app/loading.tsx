import { Skeleton, SkeletonCard } from "@/components/ui/Feedback";

/**
 * Route-level loading state.
 *
 * Shaped like the page it replaces — a header line, a toolbar row, then a card
 * grid — so content lands in the space its skeleton occupied rather than
 * reflowing the page underneath the user's cursor.
 *
 * No spinner. A skeleton communicates *what* is coming as well as *that*
 * something is; a spinner only ever says the second.
 */
export default function Loading() {
  return (
    <div className="min-h-screen bg-canvas">
      <div className="flex h-(--spacing-topbar) items-center gap-3 border-b border-line-subtle px-4 lg:px-6">
        <Skeleton className="h-3.5 w-32" />
        <div className="ml-auto flex gap-2">
          <Skeleton className="size-8 rounded-md" />
          <Skeleton className="h-8 w-16 rounded-md" />
        </div>
      </div>

      <div className="mx-auto max-w-[90rem] px-4 py-8 lg:px-6">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="mt-2 h-3 w-72" />

        <div className="mt-6 flex gap-2">
          <Skeleton className="h-8 w-64 rounded-md" />
          <Skeleton className="h-8 w-32 rounded-md" />
          <Skeleton className="h-8 w-32 rounded-md" />
        </div>

        <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {Array.from({ length: 8 }, (_, index) => (
            <SkeletonCard key={index} />
          ))}
        </div>
      </div>
    </div>
  );
}
