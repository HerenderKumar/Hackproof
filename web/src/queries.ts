// The reads every page shares, by the query keys the pages invalidate after a write.

import { useQuery } from "@tanstack/react-query";
import { get, isApiError } from "@/api";
import type { ClaimsView, CurrentSubmission, SnapshotsView, TeamView, TestsView } from "@/types";

const POLL_MS = 3000;

export const keys = {
  team: ["team"] as const,
  claims: ["claims"] as const,
  tests: ["tests"] as const,
  snapshots: ["snapshots"] as const,
  selfCheck: (jobId: string) => ["self-check", jobId] as const,
  submission: ["submission"] as const,
};

export function useTeam() {
  return useQuery({ queryKey: keys.team, queryFn: () => get<TeamView>("/team") });
}

export function useClaims() {
  return useQuery({ queryKey: keys.claims, queryFn: () => get<ClaimsView>("/team/claims") });
}

/** The tests, polled while their drafting is still under way. */
export function useTests() {
  return useQuery({
    queryKey: keys.tests,
    queryFn: () => get<TestsView>("/team/tests"),
    refetchInterval: (query) => {
      const drafting = query.state.data?.drafting;
      return drafting === "queued" || drafting === "running" ? POLL_MS : false;
    },
  });
}

/** The snapshots, polled while a commit is still being prepared. */
export function useSnapshots() {
  return useQuery({
    queryKey: keys.snapshots,
    queryFn: () => get<SnapshotsView>("/team/snapshots"),
    refetchInterval: (query) =>
      query.state.data?.pending.some((p) => p.status === "queued" || p.status === "running")
        ? POLL_MS
        : false,
  });
}

/** The current submission, or null before the first; polled while its final run is under way. */
export function useSubmission() {
  return useQuery({
    queryKey: keys.submission,
    queryFn: async () => {
      try {
        return await get<CurrentSubmission>("/team/submissions/current");
      } catch (error) {
        if (isApiError(error, "no_submission")) return null;
        throw error;
      }
    },
    refetchInterval: (query) => {
      const run = query.state.data?.final_run;
      return run === "queued" || run === "running" ? POLL_MS : false;
    },
  });
}

export { POLL_MS };
