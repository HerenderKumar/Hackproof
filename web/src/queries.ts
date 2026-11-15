// The reads every page shares, by the query keys the pages invalidate after a write.

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { get, isApiError } from "@/api";
import type {
  ClaimsView,
  CurrentSubmission,
  DevicesView,
  MatchExcerpts,
  PriorWorkView,
  SnapshotsView,
  TeamView,
  TestsView,
} from "@/types";

const POLL_MS = 3000;

export const keys = {
  team: ["team"] as const,
  claims: ["claims"] as const,
  tests: ["tests"] as const,
  snapshots: ["snapshots"] as const,
  selfCheck: (jobId: string) => ["self-check", jobId] as const,
  submission: ["submission"] as const,
  devices: ["devices"] as const,
  priorWork: ["prior-work"] as const,
  excerpts: (matchId: string) => ["excerpts", matchId] as const,
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

/** The team's devices, polled while a scan is uploading or sealed but not yet indexed. */
export function useDevices() {
  return useQuery({
    queryKey: keys.devices,
    queryFn: () => get<DevicesView>("/team/devices"),
    refetchInterval: (query) =>
      query.state.data?.devices.some(
        (d) => d.state === "uploading" || ((d.state === "complete" || d.state === "incomplete") && !d.indexed),
      )
        ? POLL_MS
        : false,
  });
}

const PAGE = 100;

/** The prior-work report, a page of matches at a time; polled while it is being made. A report
 * that is not open yet, or a team with no submission, reads as that code. */
export function usePriorWork() {
  return useInfiniteQuery({
    queryKey: keys.priorWork,
    initialPageParam: 0,
    queryFn: async ({ pageParam }): Promise<PriorWorkView | "report_not_open" | "no_submission"> => {
      try {
        return await get<PriorWorkView>(`/team/prior-work?offset=${pageParam}&limit=${PAGE}`);
      } catch (error) {
        if (isApiError(error, "report_not_open")) return "report_not_open";
        if (isApiError(error, "no_submission")) return "no_submission";
        throw error;
      }
    },
    getNextPageParam: (last, pages) => {
      if (typeof last === "string") return undefined;
      const loaded = pages.reduce((n, page) => n + (typeof page === "string" ? 0 : page.matches.length), 0);
      return loaded < last.matches_total ? loaded : undefined;
    },
    refetchInterval: (query) => {
      const first = query.state.data?.pages[0];
      return first && typeof first !== "string" && first.status === "pending" ? POLL_MS : false;
    },
  });
}

/** One match's two excerpts, loaded only once its card is opened. */
export function useMatchExcerpts(matchId: string, open: boolean) {
  return useQuery({
    queryKey: keys.excerpts(matchId),
    queryFn: () => get<MatchExcerpts>(`/team/prior-work/matches/${matchId}`),
    enabled: open,
    staleTime: Infinity,
  });
}

export { POLL_MS };
