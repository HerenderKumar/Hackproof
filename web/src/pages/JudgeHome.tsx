import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { get, isApiError } from "@/api";
import { SignedOut } from "@/components/Layout";
import { ErrorNotice, Notice, Spinner } from "@/components/ui";
import { VerdictStrip } from "@/judging/Verdicts";
import type { BriefState, JudgeTeams } from "@/judging/types";

const BRIEF: Record<BriefState, string> = {
  ok: "Brief ready",
  failed: "Brief ready (results only)",
  pending: "Brief being written",
  none: "Brief not started",
};

export function JudgeHome() {
  const teams = useQuery({ queryKey: ["judge-teams"], queryFn: () => get<JudgeTeams>("/judge/teams"), refetchInterval: 15000 });
  if (teams.isPending) return <Spinner label="Loading teams" />;
  if (teams.isError) {
    if (isApiError(teams.error) && teams.error.status === 401) return <SignedOut />;
    return <ErrorNotice error={teams.error} title="The team list could not be loaded" />;
  }
  const { event, criteria } = teams.data;
  return (
    <>
      <h1 className="text-xl font-semibold">{event.name}</h1>
      {event.scores_locked && <Notice tone="info">Scores are locked. You can still read every brief.</Notice>}
      <ul className="space-y-3">
        {teams.data.teams.map((team) => (
          <li key={team.team_id}>
            {team.submission ? (
              <Link
                to={`/judge/brief/${team.submission.id}`}
                className="block rounded-lg border border-neutral-200 bg-white p-4 shadow-xs hover:border-indigo-300"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-semibold text-neutral-900">{team.team_name}</span>
                  <span className="text-xs tabular-nums text-neutral-500">
                    {team.submission.scored}/{criteria} scored · total {team.submission.total}
                  </span>
                </div>
                <div className="mt-2">
                  <VerdictStrip verdicts={team.submission.verdicts} />
                </div>
                <p className="mt-2 text-xs text-neutral-500">{BRIEF[team.submission.brief]}</p>
              </Link>
            ) : (
              <div className="rounded-lg border border-dashed border-neutral-300 p-4 text-sm text-neutral-500">
                {team.team_name}: no submission
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  );
}
