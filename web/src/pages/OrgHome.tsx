import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";
import { get, isApiError, send } from "@/api";
import { Button, ErrorNotice, Mono, Notice, Section, Spinner } from "@/components/ui";
import { KickoffScans } from "@/judging/KickoffScans";
import { show, VerdictStrip } from "@/judging/Verdicts";
import type { BriefState, Dashboard, EventSummary, JobState, SpotCheck } from "@/judging/types";
import { when } from "@/time";

const JOB: Record<JobState, string> = { none: "—", pending: "running", done: "done", failed: "failed" };
const BRIEF: Record<BriefState, string> = { ok: "ready", failed: "results only", pending: "writing", none: "—" };

function tone(state: string): string {
  if (state === "failed") return "text-red-700 font-medium";
  if (state === "pending" || state === "running" || state === "queued" || state === "writing") return "text-sky-700";
  return "text-neutral-700";
}

/** A team's tests with the hidden final cases, opened on demand (section 5.1, step 7). */
function Specs({ teamId }: { teamId: string }) {
  const specs = useQuery({ queryKey: ["spot-check", teamId], queryFn: () => get<SpotCheck>(`/teams/${teamId}/specs`) });
  if (specs.isPending) return <Spinner small label="Loading tests" />;
  if (specs.isError) return <ErrorNotice error={specs.error} />;
  if (!specs.data.claims.length) return <p className="text-sm text-neutral-500">No claims yet.</p>;
  return (
    <div className="space-y-3">
      {specs.data.claims.map((c) => (
        <div key={c.claim_id} className="rounded-md border border-neutral-200 bg-neutral-50 p-3 text-sm">
          <p>
            <span className="font-mono">{c.claim_id}</span> {c.text}{" "}
            <span className="text-xs text-neutral-500">· {c.test ? `${c.test.claim_type}, ${c.test.status}` : c.state}</span>
          </p>
          {c.test?.reason && <p className="mt-1 text-orange-800">{c.test.reason}</p>}
          {c.test && (
            <>
              <p className="mt-2 text-xs font-medium text-neutral-600">Practice cases (the team sees these)</p>
              <Mono className="block">{show(c.test.practice_cases)}</Mono>
              <p className="mt-2 text-xs font-medium text-neutral-600">Final cases (hidden from the team)</p>
              <Mono className="block">{show(c.test.final_cases)}</Mono>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

function Teams({ board }: { board: Dashboard }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Section title={`Teams (${board.teams.length})`}>
      <div className="-mx-5 overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="text-xs text-neutral-500">
            <tr className="border-b border-neutral-200">
              <th className="px-5 py-2 font-medium">Team</th>
              <th className="px-2 py-2 font-medium">Repo</th>
              <th className="px-2 py-2 font-medium">Last poll</th>
              <th className="px-2 py-2 font-medium">Claims</th>
              <th className="px-2 py-2 font-medium">Submitted</th>
              <th className="px-2 py-2 font-medium">Run · audit · brief</th>
              <th className="px-2 py-2 font-medium">Verdicts</th>
              <th className="px-5 py-2" />
            </tr>
          </thead>
          <tbody>
            {board.teams.map((t) => (
              <Fragment key={t.id}>
                <tr className="border-b border-neutral-100 align-top">
                  <td className="px-5 py-2 font-medium">
                    {t.name}
                    {!t.rules_acknowledged && <span className="block text-xs font-normal text-amber-700">rules not acknowledged</span>}
                  </td>
                  <td className="px-2 py-2">
                    {t.repo_url ? (
                      <span className="whitespace-nowrap font-mono text-xs">{t.repo_url.replace(/^https?:\/\//, "")}</span>
                    ) : (
                      <span className="text-xs text-amber-700">not registered</span>
                    )}
                  </td>
                  <td className="px-2 py-2 text-xs">
                    {t.last_polled_at ? when(t.last_polled_at) : "—"}
                    {t.last_observed_at && <span className="block text-neutral-500">head changed {when(t.last_observed_at)}</span>}
                  </td>
                  <td className="px-2 py-2 text-xs">{t.claims_version ? `v${t.claims_version}` : "—"}</td>
                  <td className="px-2 py-2 text-xs">{t.submission ? when(t.submission.submitted_at) : "—"}</td>
                  <td className="px-2 py-2 text-xs">
                    {t.submission ? (
                      <>
                        <span className={tone(JOB[t.submission.final_run])}>{JOB[t.submission.final_run]}</span> ·{" "}
                        <span className={tone(JOB[t.submission.audit])}>{JOB[t.submission.audit]}</span> ·{" "}
                        <span className={tone(BRIEF[t.submission.brief])}>{BRIEF[t.submission.brief]}</span>
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-2 py-2">{t.submission && <VerdictStrip verdicts={t.submission.verdicts} />}</td>
                  <td className="space-x-3 whitespace-nowrap px-5 py-2 text-right text-xs">
                    {t.submission && (
                      <Link className="text-indigo-700 underline" to={`/org/brief/${t.submission.id}`}>
                        Brief
                      </Link>
                    )}
                    <button type="button" className="text-indigo-700 underline" onClick={() => setOpen(open === t.id ? null : t.id)}>
                      {open === t.id ? "Hide tests" : "Tests"}
                    </button>
                  </td>
                </tr>
                {open === t.id && (
                  <tr className="border-b border-neutral-100">
                    <td colSpan={8} className="px-5 py-3">
                      <Specs teamId={t.id} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Jobs({ board }: { board: Dashboard }) {
  const client = useQueryClient();
  const rerun = useMutation({
    mutationFn: (id: string) => send("POST", `/jobs/${id}/rerun`),
    onSuccess: () => client.invalidateQueries({ queryKey: ["dashboard"] }),
  });
  return (
    <Section title="Jobs" aside={<span className="text-xs text-neutral-500">newest {board.jobs.length}</span>}>
      <ErrorNotice error={rerun.error} />
      <ul className="divide-y divide-neutral-100 text-sm">
        {board.jobs.map((j) => (
          <li key={j.id} className="flex flex-wrap items-baseline justify-between gap-2 py-2">
            <span>
              <span className="font-mono text-xs">{j.kind}</span> <span className="text-neutral-600">{j.team_name ?? ""}</span>{" "}
              <span className={`text-xs ${tone(j.status)}`}>
                {j.status}
                {j.attempts > 1 && ` after ${j.attempts} attempts`}
              </span>
              {j.error_detail && (
                <span className="block text-xs text-red-700">
                  {j.error_class}: {j.error_detail}
                </span>
              )}
            </span>
            <span className="flex items-center gap-3 text-xs text-neutral-500">
              {when(j.finished_at ?? j.created_at)}
              {(j.status === "done" || j.status === "failed") && (
                <Button
                  variant="secondary"
                  className="px-2 py-1 text-xs"
                  busy={rerun.isPending && rerun.variables === j.id}
                  onClick={() => rerun.mutate(j.id)}
                >
                  Rerun
                </Button>
              )}
            </span>
          </li>
        ))}
      </ul>
    </Section>
  );
}

function Scores({ board }: { board: Dashboard }) {
  const client = useQueryClient();
  const lock = useMutation({
    mutationFn: () => send("POST", `/events/${board.event.id}/lock-scores`),
    onSuccess: () => client.invalidateQueries({ queryKey: ["dashboard"] }),
  });
  return (
    <Section title="Judges and scores">
      <ul className="space-y-1 text-sm">
        {board.judges.map((j) => (
          <li key={j.id}>
            <span className="font-medium">{j.name}</span>{" "}
            <span className="text-neutral-600">
              · opened {j.briefs_opened} · scored {j.submissions_scored}
            </span>
          </li>
        ))}
        {!board.judges.length && <li className="text-neutral-500">No judges yet.</li>}
      </ul>
      <ErrorNotice error={lock.error} />
      <div className="flex flex-wrap gap-2">
        {board.event.scores_locked ? (
          <Notice tone="info">Scores are locked.</Notice>
        ) : (
          <Button
            variant="danger"
            busy={lock.isPending}
            onClick={() => {
              if (window.confirm("Lock every judge's scores? They cannot be changed afterwards.")) lock.mutate();
            }}
          >
            Lock scores
          </Button>
        )}
        <a
          className="inline-flex items-center rounded-md border border-neutral-300 bg-white px-3.5 py-2 text-sm font-medium text-neutral-800 hover:bg-neutral-50"
          href={`/api/events/${board.event.id}/scores.csv`}
          download
        >
          Export scores (CSV)
        </a>
      </div>
    </Section>
  );
}

/** The organiser's dashboard (section 5.1, steps 6 to 8) for the event in `?event=<id>`, or
 * the one made most recently. */
export function OrgHome() {
  const [params] = useSearchParams();
  const events = useQuery({ queryKey: ["events"], queryFn: () => get<{ events: EventSummary[] }>("/events") });
  const all = events.data?.events ?? [];
  const asked = params.get("event");
  const eventId = (all.find((e) => e.id === asked) ?? all[0])?.id;
  const others = all.filter((e) => e.id !== eventId);
  const board = useQuery({
    queryKey: ["dashboard", eventId],
    queryFn: () => get<Dashboard>(`/events/${eventId}/dashboard`),
    enabled: !!eventId,
    refetchInterval: 10000,
  });
  const failure = events.error ?? board.error;
  if (failure) {
    if (isApiError(failure) && (failure.status === 401 || failure.status === 403)) return <Navigate to="/login" replace />;
    return <ErrorNotice error={failure} title="The dashboard could not be loaded" />;
  }
  if (events.isPending) return <Spinner label="Loading" />;
  if (!eventId) return <Notice tone="info" title="No event yet">Create an event through the API first (section 17).</Notice>;
  if (!board.data) return <Spinner label="Loading the dashboard" />;
  const b = board.data;
  const failed = b.jobs.filter((j) => j.status === "failed").length;
  return (
    <>
      <header>
        <h1 className="text-xl font-semibold">{b.event.name}</h1>
        <p className="mt-1 text-sm text-neutral-600">
          Kickoff {when(b.event.kickoff_at)} · deadline {when(b.event.deadline_at)}
        </p>
        {others.length > 0 && (
          <details className="mt-1 text-sm text-neutral-600">
            <summary className="cursor-pointer select-none">Other events ({others.length})</summary>
            <p className="mt-1">
              {others.map((e, i) => (
                <Fragment key={e.id}>
                  {i > 0 && " · "}
                  <Link className="underline" to={`/org?event=${e.id}`}>
                    {e.name}
                  </Link>
                </Fragment>
              ))}
            </p>
          </details>
        )}
      </header>
      {failed > 0 && <Notice tone="error">{failed} of the shown jobs failed. Each can be run again below.</Notice>}
      <Teams board={b} />
      <Scores board={b} />
      <details className="rounded-lg border border-neutral-200 bg-white px-5 py-3 shadow-xs">
        <summary className="cursor-pointer select-none text-base font-semibold text-neutral-900">
          Kickoff scans and starter code
        </summary>
        <div className="mt-3">
          <KickoffScans eventId={b.event.id} />
        </div>
      </details>
      <details className="rounded-lg border border-neutral-200 bg-white px-5 py-3 shadow-xs">
        <summary className="cursor-pointer select-none text-base font-semibold text-neutral-900">
          Job log <span className="text-sm font-normal text-neutral-500">({b.jobs.length} newest{failed > 0 ? `, ${failed} failed` : ""})</span>
        </summary>
        <div className="mt-3">
          <Jobs board={b} />
        </div>
      </details>
    </>
  );
}
