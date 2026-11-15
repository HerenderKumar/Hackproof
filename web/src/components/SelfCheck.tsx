import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import { get, getText, isApiError, newKey, send } from "@/api";
import { inWords, shown } from "@/components/TestCard";
import { Button, ErrorNotice, Field, Mono, Notice, Section, Spinner, StateBadge } from "@/components/ui";
import { CaseVideo, PageDetails, TraceLink, UiSteps } from "@/components/UiEvidence";
import { keys, POLL_MS, useSnapshots } from "@/queries";
import { when } from "@/time";
import type { ResultOut, SelfCheckStart, SelfCheckView, SnapshotRequest, TestsView } from "@/types";

const ACTIVE = "hp:self-check"; // the self-check being watched, kept across reloads of this tab

function remembered(): string | null {
  try {
    return window.sessionStorage.getItem(ACTIVE);
  } catch {
    return null;
  }
}

function remember(jobId: string | null) {
  try {
    if (jobId) window.sessionStorage.setItem(ACTIVE, jobId);
    else window.sessionStorage.removeItem(ACTIVE);
  } catch {
    // private windows may refuse storage; watching still works until a reload
  }
}

const short = (sha: string) => sha.slice(0, 10);

// --- taking snapshots ------------------------------------------------------------------------------

function TakeSnapshot() {
  const queryClient = useQueryClient();
  const [sha, setSha] = useState("");
  const take = useMutation({
    mutationFn: (commit_sha?: string) =>
      send<SnapshotRequest>("POST", "/team/snapshots", commit_sha ? { commit_sha } : {}),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: keys.snapshots }),
    onSuccess: () => {
      setSha("");
      void queryClient.invalidateQueries({ queryKey: keys.team });
    },
  });

  function paste(event: FormEvent) {
    event.preventDefault();
    take.mutate(sha.trim());
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <Button onClick={() => take.mutate(undefined)} busy={take.isPending && !take.variables}>
          Snapshot the latest commit
        </Button>
        <form onSubmit={paste} className="flex flex-1 flex-wrap items-end gap-2">
          <Field
            className="min-w-64 flex-1"
            label="Or a commit SHA"
            value={sha}
            onChange={setSha}
            placeholder="the full 40-character SHA"
            spellCheck={false}
            autoComplete="off"
          />
          <Button type="submit" variant="secondary" disabled={!sha.trim()} busy={take.isPending && !!take.variables}>
            Snapshot this commit
          </Button>
        </form>
      </div>
      {take.data && (
        <Notice tone={take.data.status === "ready" ? "ok" : "info"}>
          {take.data.status === "ready"
            ? `Commit ${short(take.data.commit_sha)} is already snapshotted.`
            : `Preparing a snapshot of ${short(take.data.commit_sha)}…`}
        </Notice>
      )}
      <ErrorNotice error={take.error} />
    </div>
  );
}

// --- one self-check's result --------------------------------------------------------------------------

function UiResult({ jobId, result }: { jobId: string; result: ResultOut }) {
  const base = `/api/team/self-checks/${jobId}/results/${result.result_id}/media`;
  return (
    <div className="mt-3 space-y-3 rounded-md border border-neutral-200 bg-neutral-50 p-3">
      <p className="text-xs text-neutral-500">Practice case {result.case_id ?? "—"} in a browser</p>
      <CaseVideo base={base} hasVideo={result.has_video} />
      <UiSteps steps={result.steps ?? []} />
      {result.verdict !== "pass" && result.error && <p className="text-sm text-neutral-700">{result.error}</p>}
      {result.verdict !== "pass" && <PageDetails page={result.page} open={result.verdict === "unverified"} />}
      <TraceLink base={base} hasTrace={result.has_trace} />
    </div>
  );
}

function FailedAssertions({ result }: { result: ResultOut }) {
  const failed = (result.assertion_results ?? []).filter((a) => a.passed === false);
  if (result.verdict === "pass") return null;
  return (
    <div className="mt-2 space-y-1 text-sm">
      {result.error && <p className="text-neutral-700">{result.error}</p>}
      {failed.map((assertion, i) => (
        <div key={i} className="rounded bg-red-50 px-3 py-2 text-xs text-red-900">
          <p className="font-mono">{inWords({ op: assertion.op, path: assertion.path ?? undefined, value: assertion.expected })}</p>
          <p className="mt-1">
            expected <Mono>{shown(assertion.expected)}</Mono>, got <Mono>{shown(assertion.actual)}</Mono>
            {assertion.note ? ` (${assertion.note})` : ""}
          </p>
        </div>
      ))}
    </div>
  );
}

function Log({ jobId, kind }: { jobId: string; kind: "build" | "app" }) {
  const [open, setOpen] = useState(false);
  const log = useQuery({
    queryKey: ["self-check-log", jobId, kind],
    queryFn: () => getText(`/team/self-checks/${jobId}/logs/${kind}`),
    enabled: open,
    retry: false,
  });
  return (
    <div className="space-y-2">
      <Button variant="secondary" onClick={() => setOpen((v) => !v)}>
        {open ? "Hide" : "Show"} the {kind === "build" ? "build" : "app"} log
      </Button>
      {open &&
        (log.isPending ? (
          <Spinner />
        ) : log.isError ? (
          <ErrorNotice error={log.error} />
        ) : (
          <pre className="max-h-96 overflow-auto rounded bg-neutral-900 px-3 py-2 font-mono text-xs text-neutral-100">
            {log.data || "The log is empty."}
          </pre>
        ))}
    </div>
  );
}

function stageText(status: string, ok: string, fail: string): string {
  if (status === "ok") return ok;
  if (status === "fail") return fail;
  return "skipped";
}

function Result({ jobId, claims, onGone }: { jobId: string; claims: TestsView | undefined; onGone: () => void }) {
  const check = useQuery({
    queryKey: keys.selfCheck(jobId),
    queryFn: () => get<SelfCheckView>(`/team/self-checks/${jobId}`),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === undefined || status === "queued" || status === "running" ? POLL_MS : false;
    },
  });
  const gone = check.isError && isApiError(check.error) && check.error.status === 404;
  useEffect(() => {
    if (gone) onGone(); // a check remembered from another sign-in in this tab
  }, [gone, onGone]);
  if (check.isPending || gone) return check.isPending ? <Spinner label="Loading the self-check" /> : null;
  if (check.isError) return <ErrorNotice error={check.error} />;
  const view = check.data;
  const textOf = (id: string) => claims?.claims.find((c) => c.claim_id === id)?.text ?? "";

  if (view.status === "queued" || view.status === "running") {
    return (
      <Spinner
        label={`Self-check of ${short(view.commit_sha ?? "")} ${view.status === "queued" ? "waiting to start" : "running"}…`}
      />
    );
  }
  if (!view.run) {
    return (
      <Notice tone="error" title="The self-check could not run">
        {view.error ?? "Something went wrong on HackProof's side. Try again."}
      </Notice>
    );
  }
  const { run } = view;
  return (
    <div className="space-y-4">
      <p className="text-sm text-neutral-600">
        Self-check of <Mono>{short(view.commit_sha ?? "")}</Mono>
      </p>
      <dl className="grid gap-3 sm:grid-cols-2">
        <div className="rounded-md border border-neutral-200 px-3 py-2">
          <dt className="text-xs uppercase tracking-wide text-neutral-500">Build</dt>
          <dd className="text-sm font-medium">{stageText(run.build_status, "built", "the build failed")}</dd>
        </div>
        <div className="rounded-md border border-neutral-200 px-3 py-2">
          <dt className="text-xs uppercase tracking-wide text-neutral-500">Startup</dt>
          <dd className="text-sm font-medium">{stageText(run.startup_status, "started", "the app did not start")}</dd>
          {run.startup_status !== "ok" && run.startup_detail && (
            <dd className="mt-1 text-xs text-neutral-600">{run.startup_detail}</dd>
          )}
        </div>
      </dl>
      <ul className="space-y-3">
        {Object.entries(view.claim_verdicts).map(([claimId, verdict]) => (
          <li key={claimId} className="rounded-md border border-neutral-200 px-3 py-2">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <p className="text-sm">
                <span className="mr-2 font-mono text-xs text-neutral-500">{claimId}</span>
                {textOf(claimId)}
              </p>
              <StateBadge state={verdict} />
            </div>
            {view.results
              .filter((result) => result.claim_id === claimId)
              .map((result) =>
                result.kind === "ui" ? (
                  <UiResult key={result.result_id} jobId={jobId} result={result} />
                ) : (
                  <FailedAssertions key={result.result_id} result={result} />
                ),
              )}
          </li>
        ))}
      </ul>
      {Object.keys(view.claim_verdicts).length === 0 && (
        <p className="text-sm text-neutral-600">No test ran: none of your claims has a test that can run yet.</p>
      )}
      <div className="flex flex-wrap gap-3">
        <Log jobId={jobId} kind="build" />
        <Log jobId={jobId} kind="app" />
      </div>
    </div>
  );
}

// --- the section -------------------------------------------------------------------------------------

export function SelfCheck({ tests }: { tests: TestsView | undefined }) {
  const queryClient = useQueryClient();
  const snapshots = useSnapshots();
  const [active, setActive] = useState<string | null>(remembered);
  useEffect(() => remember(active), [active]);
  const forget = useCallback(() => setActive(null), []);

  const start = useMutation({
    mutationFn: (snapshotId: string) =>
      send<SelfCheckStart>("POST", `/team/snapshots/${snapshotId}/self-check`, { client_request_id: newKey() }),
    onSuccess: (started) => {
      setActive(started.job_id);
      void queryClient.invalidateQueries({ queryKey: keys.selfCheck(started.job_id) });
    },
    onError: (error) => {
      // One self-check at a time: watch the one already running.
      if (isApiError(error, "self_check_in_progress") && typeof error.detail.job_id === "string") {
        setActive(error.detail.job_id);
      }
    },
  });

  return (
    <Section title="Self-check">
      <p className="text-sm text-neutral-600">
        Snapshot a commit, then run it against the practice case of each test. A self-check never uses the
        hidden cases and does not count toward judging.
      </p>
      <TakeSnapshot />
      {snapshots.isError && <ErrorNotice error={snapshots.error} />}
      {snapshots.data && (
        <div className="space-y-2">
          {snapshots.data.pending.map((pending) => (
            <div key={pending.commit_sha} className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span>
                <Mono>{short(pending.commit_sha)}</Mono>{" "}
                {pending.status === "queued" || pending.status === "running" ? (
                  <Spinner small label="being prepared" />
                ) : (
                  <span className="text-red-800">
                    {pending.status === "refused" ? "refused" : "failed"}
                    {pending.detail ? `: ${pending.detail}` : ""}
                  </span>
                )}
              </span>
            </div>
          ))}
          {snapshots.data.snapshots.length === 0 && snapshots.data.pending.length === 0 && (
            <p className="text-sm text-neutral-500">No snapshot yet.</p>
          )}
          <ul className="divide-y divide-neutral-200 rounded-md border border-neutral-200">
            {snapshots.data.snapshots.map((snapshot) => (
              <li key={snapshot.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                <span className="text-sm">
                  <Mono>{short(snapshot.commit_sha)}</Mono>
                  <span className="ml-2 text-neutral-500">taken {when(snapshot.created_at)}</span>
                </span>
                <Button
                  variant="secondary"
                  onClick={() => start.mutate(snapshot.id)}
                  busy={start.isPending && start.variables === snapshot.id}
                  disabled={start.isPending}
                >
                  Run self-check
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {start.isError && !isApiError(start.error, "self_check_in_progress") && <ErrorNotice error={start.error} />}
      {active && (
        <div className="border-t border-neutral-200 pt-4">
          <Result jobId={active} claims={tests} onGone={forget} />
        </div>
      )}
    </Section>
  );
}
