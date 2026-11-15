import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { get, send } from "@/api";
import { Button, ErrorNotice, Mono, Notice, Section, Spinner, StateBadge } from "@/components/ui";
import { CaseVideo, PageDetails, TraceLink, UiSteps } from "@/components/UiEvidence";
import { readable, VerdictStrip } from "@/judging/Verdicts";
import type { Brief as BriefData, BriefClaim, Case, Citation, Mark, Question } from "@/judging/types";
import { when } from "@/time";
import type { PriorWorkView, Verdict } from "@/types";

const IMPLEMENTATION: Record<string, string> = {
  implemented: "Code for it is there",
  partial: "Part of it is there",
  not_found: "No code for it found",
  unclear: "Could not tell",
};

function briefKey(id: string) {
  return ["brief", id] as const;
}

function UiEvidence({ item }: { item: Case }) {
  const base = `/api/judge/results/${item.result_id}/media`;
  return (
    <div className="space-y-3 rounded-md border border-neutral-200 bg-neutral-50 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <StateBadge state={item.verdict} />
        <span className="text-xs text-neutral-500">
          case {item.case_id ?? "—"} in a browser
          {item.input !== null && item.input !== undefined && <> · input <Mono>{readable(item.input)}</Mono></>}
          {item.duration_ms !== null && ` · ${(item.duration_ms / 1000).toFixed(1)} s`}
        </span>
      </div>
      <CaseVideo base={base} hasVideo={item.media?.video ?? false} />
      <UiSteps steps={item.steps ?? []} />
      {item.error && <p className="text-neutral-700">{item.error}</p>}
      {item.verdict !== "pass" && <PageDetails page={item.page} />}
      <TraceLink base={base} hasTrace={item.media?.trace ?? false} />
    </div>
  );
}

function Evidence({ item }: { item: Case }) {
  if (item.kind === "ui") return <UiEvidence item={item} />;
  return (
    <div className="rounded-md border border-neutral-200 bg-neutral-50 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <StateBadge state={item.verdict} />
        <span className="text-xs text-neutral-500">
          case {item.case_id ?? "—"} · {item.assertions_total} checks
          {item.duration_ms !== null && ` · ${item.duration_ms} ms`}
        </span>
      </div>
      {item.request && (
        <p className="mt-2">
          <Mono>
            {item.request.method} {item.request.path}
          </Mono>
          {item.request.json !== undefined && item.request.json !== null && (
            <Mono className="mt-1 block text-neutral-600">{readable(item.request.json)}</Mono>
          )}
        </p>
      )}
      {item.response?.status !== undefined && (
        <p className="mt-1 text-xs text-neutral-600">
          answered {item.response.status}: <Mono>{readable(item.response.body ?? item.response.text)}</Mono>
        </p>
      )}
      {item.failed.map((a, i) => (
        <p key={i} className="mt-2 text-red-800">
          <span className="font-medium">{a.op.replace(/_/g, " ").replace(/^./, (ch) => ch.toUpperCase())}</span>
          {a.path && <Mono className="mx-1">{a.path}</Mono>}: expected <Mono>{readable(a.expected)}</Mono>, got{" "}
          <Mono>{readable(a.actual)}</Mono>
          {a.note && <span className="block text-xs text-red-700">{a.note}</span>}
        </p>
      ))}
      {item.error && <p className="mt-2 text-neutral-700">{item.error}</p>}
    </div>
  );
}

function CitationLink({ citation }: { citation: Citation }) {
  const label = `${citation.path} lines ${citation.start_line}–${citation.end_line}`;
  return (
    <li id={`citation-${citation.id}`} className="text-sm">
      {citation.url ? (
        <a className="font-mono text-indigo-700 underline break-all" href={citation.url} target="_blank" rel="noreferrer">
          {label}
        </a>
      ) : (
        <Mono>{label}</Mono>
      )}
      <span className="text-neutral-600"> · {citation.why}</span>
    </li>
  );
}

function MarkButtons({ brief, claim }: { brief: BriefData; claim: BriefClaim }) {
  const client = useQueryClient();
  const current = brief.own.marks[claim.id];
  const save = useMutation({
    mutationFn: (mark: Mark | "none") =>
      send("PUT", `/judge/submissions/${brief.submission.id}/claim-marks`, { claim_id: claim.id, mark }),
    onSuccess: () => client.invalidateQueries({ queryKey: briefKey(brief.submission.id) }),
  });
  const choice = (mark: Mark, label: string) => (
    <Button
      variant={current === mark ? "primary" : "secondary"}
      className="min-h-11 flex-1"
      busy={save.isPending && save.variables === mark}
      onClick={() => save.mutate(current === mark ? "none" : mark)}
      aria-pressed={current === mark}
    >
      {label}
    </Button>
  );
  return (
    <div>
      <div className="flex gap-2">
        {choice("verified_live", "Verified live")}
        {choice("not_verified", "Not verified")}
      </div>
      <ErrorNotice error={save.error} />
    </div>
  );
}

function ClaimCard({ brief, claim }: { brief: BriefData; claim: BriefClaim }) {
  const line = brief.written.claims[claim.id];
  const map = claim.code_map;
  const [open, setOpen] = useState(claim.verdict !== "pass");
  return (
    <Section
      title={`${claim.id.replace(/^c(\d+)$/, "Claim $1")}: ${claim.text}`}
      aside={<StateBadge state={claim.verdict} />}
    >
      <p className="text-xs text-neutral-500">
        <span className="rounded-md bg-indigo-50 px-2 py-0.5 font-medium text-indigo-700">{(claim.type ?? "claim").replace(/_/g, " ").replace(/^./, (ch) => ch.toUpperCase())}</span> · <Mono>{claim.target}</Mono>
      </p>
      {line && <p className="text-sm text-neutral-800">{line}</p>}
      <button type="button" className="text-sm font-medium text-indigo-700 underline" onClick={() => setOpen(!open)}>
        {open ? "Hide" : "Show"} test evidence ({claim.cases.length})
      </button>
      {open && (
        <div className="space-y-2">
          {claim.cases.length ? (
            claim.cases.map((item) => (
              <div key={item.result_id} id={`result-${item.result_id}`}>
                <Evidence item={item} />
              </div>
            ))
          ) : (
            <p className="text-sm text-neutral-600">No test ran for this claim.</p>
          )}
        </div>
      )}
      <div>
        <h3 className="text-sm font-semibold text-neutral-800">Code</h3>
        {claim.audit ? (
          <>
            <p className="text-sm text-neutral-700">
              {IMPLEMENTATION[claim.audit.implementation] ?? claim.audit.implementation}: {claim.audit.explanation}
            </p>
            <ul className="mt-1 space-y-1">
              {claim.audit.citations.map((c) => (
                <CitationLink key={c.id} citation={c} />
              ))}
            </ul>
          </>
        ) : (
          <p className="text-sm text-neutral-500">The code reading has nothing for this claim.</p>
        )}
        {map && map.method === "graph" && map.neighbours.length > 0 && (
          <p className="mt-2 text-xs text-neutral-600">
            Related:{" "}
            {map.neighbours.slice(0, 8).map((n, i) => (
              <span key={i} className="mr-2 font-mono">
                {n.label}
                {n.explicit === false && "?"}
              </span>
            ))}
          </p>
        )}
        {map && map.method === "search" && map.hits.length > 0 && (
          <p className="mt-2 text-xs text-neutral-600">
            Route found in:{" "}
            {map.hits.slice(0, 5).map((h, i) => (
              <span key={i} className="mr-2 font-mono">
                {h.path}:{h.line}
              </span>
            ))}
          </p>
        )}
      </div>
      {brief.can_score && <MarkButtons brief={brief} claim={claim} />}
    </Section>
  );
}

function QuestionItem({ q }: { q: Question }) {
  const target = q.ref ? `#${q.ref.kind === "citation" ? "citation" : "result"}-${q.ref.id}` : null;
  return (
    <li className="text-sm text-neutral-800">
      {q.question}
      {target && (
        <a className="ml-2 text-xs text-indigo-700 underline" href={target}>
          {q.ref?.kind === "citation" ? "see the code" : "see the test"}
        </a>
      )}
    </li>
  );
}

function Timeline({ brief }: { brief: BriefData }) {
  const points = brief.growth.map((g) => ({ t: new Date(g.observed_at).getTime(), lines: g.lines }));
  const kickoff = new Date(brief.kickoff_at).getTime();
  if (points.length > 0 && (points[0]?.t ?? 0) > kickoff) points.unshift({ t: kickoff, lines: 0 });
  return (
    <Section title="Build timeline" aside={<span className="text-xs text-neutral-500">Context for questions, not a verdict</span>}>
      {points.length > 1 ? (
        <div className="h-48">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={points} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" />
              <XAxis
                dataKey="t"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(t: number) => new Date(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
                fontSize={11}
              />
              <YAxis fontSize={11} width={44} />
              <Tooltip labelFormatter={(t) => when(new Date(Number(t)).toISOString())} />
              <ReferenceLine x={kickoff} stroke="#4f46e5" label={{ value: "kickoff", position: "insideTopLeft", fontSize: 11, fill: "#4f46e5" }} />
              <Line type="stepAfter" dataKey="lines" stroke="#4f46e5" dot={false} name="lines of code" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <p className="text-sm text-neutral-600">Not enough repository observations to draw growth.</p>
      )}
      {brief.signals.length > 0 ? (
        <ul className="space-y-1 text-sm">
          {brief.signals.map((s, i) => (
            <li key={i}>
              <span className="mr-1 rounded-md bg-amber-50 px-2 py-0.5 font-semibold text-amber-800 ring-1 ring-amber-200">{s.kind.replaceAll("_", " ").replace(/^./, (ch) => ch.toUpperCase())}</span>
              {s.detail && <span className="text-neutral-600"> · {s.detail}</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-neutral-600">No prior-work signals.</p>
      )}
      {brief.observations.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-neutral-700">Push log ({brief.observations.length} heads seen)</summary>
          <ul className="mt-1 space-y-0.5 font-mono text-xs text-neutral-700">
            {brief.observations.map((o, i) => (
              <li key={i}>
                {when(o.observed_at)} · {o.kind.replaceAll("_", " ")} · {o.head_sha ? o.head_sha.slice(0, 7) : "no commit"}
                {o.kind === "poll" && o.lines_added !== null && ` · +${o.lines_added} lines`}
                {o.descends_from_previous === false && (
                  <span className="text-amber-700"> · replaced {o.previous_head_sha ? o.previous_head_sha.slice(0, 7) : "none"}</span>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </Section>
  );
}

function Scoring({ brief }: { brief: BriefData }) {
  const client = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  useEffect(() => {
    setValues(Object.fromEntries(Object.entries(brief.own.scores).map(([k, v]) => [k, String(v)])));
  }, [brief.own.scores]);
  const save = useMutation({
    mutationFn: () =>
      send("PUT", `/judge/submissions/${brief.submission.id}/scores`, {
        scores: Object.fromEntries(
          Object.entries(values)
            .filter(([, v]) => v !== "")
            .map(([k, v]) => [k, Number(v)]),
        ),
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: briefKey(brief.submission.id) }),
  });
  const filled = Object.values(values).some((v) => v !== "");
  return (
    <Section title="Scores" aside={<span className="text-sm font-medium tabular-nums">Total {brief.own.total}</span>}>
      {brief.scores_locked && <Notice tone="info">The organiser has locked the scores.</Notice>}
      {brief.rubric.map((c) => (
        <label key={c.id} className="flex items-center justify-between gap-3 text-sm">
          <span>
            <span className="font-medium">{c.name}</span>
            {c.description && <span className="block text-xs text-neutral-500">{c.description}</span>}
          </span>
          <span className="flex items-center gap-1">
            <input
              type="number"
              inputMode="numeric"
              min={0}
              max={c.max}
              disabled={brief.scores_locked}
              className="h-11 w-20 rounded-md border border-neutral-300 px-2 text-right tabular-nums"
              value={values[c.id] ?? ""}
              onChange={(e) => setValues({ ...values, [c.id]: e.target.value })}
            />
            <span className="text-neutral-500">/ {c.max}</span>
          </span>
        </label>
      ))}
      <ErrorNotice error={save.error} />
      {!brief.scores_locked && (
        <Button className="min-h-11 w-full" busy={save.isPending} disabled={!filled} onClick={() => save.mutate()}>
          Save scores
        </Button>
      )}
      {save.isSuccess && <p className="text-sm text-emerald-700">Saved.</p>}
    </Section>
  );
}

const DECLARATION: Record<string, string> = {
  starter_code: "Used starter code",
  earlier_work: "Brought earlier work",
  ai_assistance: "Used AI assistance",
};

/** The prior-work report in one line, with a link to it (M9). Context, never a verdict. */
function PriorWorkLine({ submissionId, home }: { submissionId: string; home: string }) {
  const view = useQuery({
    queryKey: ["prior-work-line", submissionId],
    queryFn: () => get<PriorWorkView>(`/judge/submissions/${submissionId}/prior-work?limit=1`),
    refetchInterval: (q) => (q.state.data?.status === "pending" ? 5000 : false),
  });
  const v = view.data;
  let text = "Loading…";
  if (view.isError) text = "The report could not be loaded.";
  else if (v?.status === "pending") text = "The report is being made.";
  else if (v?.status === "no_baseline") text = "No device of the team was scanned at kickoff.";
  else if (v?.status === "failed") text = "The report could not be made.";
  else if (v?.totals) {
    text =
      v.totals.percent === null
        ? "No code to compare after leaving out starter and common code."
        : `${v.totals.percent}% of the final code matched code on the team's devices at kickoff, in ${v.matches_total} ${
            v.matches_total === 1 ? "match" : "matches"
          }.`;
    if (v.totals.late_percent) text += ` A further ${v.totals.late_percent}% from a late scan.`;
    if (v.explanation) text += " The team explained it.";
  }
  return (
    <Section title="Prior work" aside={<span className="text-xs text-neutral-500">Context for questions, not a verdict</span>}>
      <p className="text-sm text-neutral-800">{text}</p>
      {v?.status === "ready" && (
        <Link to={`${home}/prior-work/${submissionId}`} className="text-sm text-indigo-700 underline">
          Open the report, with the old and new code side by side
        </Link>
      )}
    </Section>
  );
}

export function Brief() {
  const { submissionId = "" } = useParams();
  const brief = useQuery({
    queryKey: briefKey(submissionId),
    queryFn: () => get<BriefData>(`/judge/submissions/${submissionId}/brief`),
    refetchInterval: (q) => {
      const d = q.state.data;
      return d && (d.ai_status === "pending" || d.run.status === "pending" || d.audit.status === "pending") ? 5000 : false;
    },
  });
  if (brief.isPending) return <Spinner label="Loading the brief" />;
  if (brief.isError) return <ErrorNotice error={brief.error} title="The brief could not be loaded" />;
  const b = brief.data;
  const verdicts: Record<string, Verdict> = Object.fromEntries(b.claims.map((c) => [c.id, c.verdict]));
  const aiProblem = b.ai_status === "failed" || b.audit.status === "failed" || b.audit.status === "partial";
  return (
    <div className="space-y-4">
      <Link to={b.can_score ? "/judge" : "/org"} className="text-sm text-indigo-700 underline">
        ← All teams
      </Link>
      <header>
        <h1 className="bg-gradient-to-r from-indigo-700 via-violet-700 to-fuchsia-700 bg-clip-text text-3xl font-bold text-transparent">{b.submission.team_name}</h1>
        <p className="mt-1 text-sm text-neutral-600">
          {b.submission.mode.replace(/_/g, " ").replace(/^./, (ch) => ch.toUpperCase())} · Submitted {when(b.submission.submitted_at)} · commit{" "}
          {b.submission.commit_url ? (
            <a className="font-mono text-indigo-700 underline" href={b.submission.commit_url} target="_blank" rel="noreferrer">
              {b.submission.commit_sha.slice(0, 10)}
            </a>
          ) : (
            <Mono>{b.submission.commit_sha.slice(0, 10)}</Mono>
          )}
        </p>
        <details className="mt-1 text-xs text-neutral-500">
          <summary className="cursor-pointer select-none">Build fingerprints</summary>
          <p className="mt-1 break-all">
            Platform-built image from archive <Mono className="text-neutral-600">{b.submission.archive_sha256}</Mono>
            {b.run.image_digest && (
              <>
                , image <Mono className="text-neutral-600">{b.run.image_digest}</Mono>
              </>
            )}
            .
          </p>
        </details>
        {b.run.model_use && (
          <p className="mt-1 text-xs text-neutral-600">
            {b.run.model_use.calls === 0
              ? "The app made no model calls during the final run."
              : `The app made ${b.run.model_use.calls} model ${b.run.model_use.calls === 1 ? "call" : "calls"} through Prune during the final run (${Object.entries(
                  b.run.model_use.models,
                )
                  .map(([m, n]) => `${m} ${n}`)
                  .join(", ") || "none answered"}).`}
            {Object.keys(b.run.model_use.outside_hosts).length > 0 &&
              ` It called outside hosts: ${Object.entries(b.run.model_use.outside_hosts)
                .map(([h, n]) => `${h} ${n}`)
                .join(", ")}.`}
            {b.run.model_use.refused.length > 0 &&
              ` Refused: ${b.run.model_use.refused.map((r) => r.reason).join("; ")}.`}
            {b.run.model_use.receipt_verified === false && " Prune's receipt could not be checked."} Context, not a
            verdict.
          </p>
        )}
      </header>
      {aiProblem && (
        <Notice tone="warn" title="Part of the AI analysis is missing">
          The test results below are complete. {b.audit.failure ?? "The written summary or the code reading did not finish;"} questions
          come from the results.
        </Notice>
      )}
      {b.run.status !== "done" && (
        <Notice tone={b.run.status === "failed" ? "error" : "info"}>
          {b.run.status === "pending" ? "The final run is still going." : `The final run did not finish: ${b.run.error ?? "no detail"}.`}
        </Notice>
      )}
      <div className="sticky top-0 z-10 -mx-4 border-b border-neutral-200 bg-neutral-50/95 px-4 py-2 backdrop-blur">
        <VerdictStrip verdicts={verdicts} />
      </div>
      {b.written.summary && <p className="text-base leading-relaxed text-neutral-800">{b.written.summary}</p>}
      {b.run.startup_status === "fail" && (
        <Notice tone="error" title="The app did not start">
          {b.run.startup_detail}
        </Notice>
      )}
      {b.claims.map((c) => (
        <ClaimCard key={c.id} brief={b} claim={c} />
      ))}
      <Section title="Two questions to ask">
        <ol className="list-decimal space-y-2 pl-5">
          {b.written.demo_questions.map((q, i) => (
            <QuestionItem key={i} q={q} />
          ))}
        </ol>
      </Section>
      {b.audit.text_hints.length > 0 && (
        <Section title="Text in the code aimed at reviewers">
          <ul className="space-y-1 text-sm">
            {b.audit.text_hints.map((h, i) => (
              <li key={i}>
                <Mono>
                  {h.path}:{h.line}
                </Mono>{" "}
                <span className="text-neutral-600">“{h.excerpt}”</span>
              </li>
            ))}
          </ul>
          <p className="text-xs text-neutral-500">Reported, never obeyed.</p>
        </Section>
      )}
      <Timeline brief={b} />
      <PriorWorkLine submissionId={submissionId} home={b.can_score ? "/judge" : "/org"} />
      <Section title="What the team declared">
        <ul className="space-y-1 text-sm">
          {Object.entries(DECLARATION).map(([key, label]) => (
            <li key={key}>
              {label}: <span className="font-medium">{b.reuse_declaration[key] ? "yes" : "no"}</span>
            </li>
          ))}
        </ul>
        {typeof (b.reuse_declaration.notes ?? b.reuse_declaration.details) === "string" &&
          String(b.reuse_declaration.notes ?? b.reuse_declaration.details) && (
            <p className="whitespace-pre-wrap text-sm text-neutral-700">
              {String(b.reuse_declaration.notes ?? b.reuse_declaration.details)}
            </p>
          )}
      </Section>
      {b.can_score && <Scoring brief={b} />}
    </div>
  );
}
