import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type ReactNode, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { isApiError, newKey, send } from "@/api";
import { Button, ErrorNotice, Mono, Notice, Section, Spinner, TextArea } from "@/components/ui";
import { keys, useSnapshots, useSubmission, useTeam, useTests } from "@/queries";
import { isPast, remaining, useNow, when } from "@/time";
import type { Receipt, ReuseDeclaration, SubmissionIn } from "@/types";

const NOTES_MAX = 2000;

function Need({ met, children }: { met: boolean; children: ReactNode }) {
  return (
    <li className="flex items-start gap-2 text-sm">
      <span aria-hidden="true" className={met ? "text-emerald-700" : "text-neutral-400"}>
        {met ? "✓" : "○"}
      </span>
      <span className={met ? "text-neutral-800" : "text-neutral-600"}>
        <span className="sr-only">{met ? "Done: " : "Not yet: "}</span>
        {children}
      </span>
    </li>
  );
}

function Refusal({ error }: { error: unknown }) {
  if (isApiError(error, "tests_not_approved") && Array.isArray(error.detail.claims)) {
    const claims = error.detail.claims.filter((c): c is string => typeof c === "string");
    return (
      <Notice tone="error" title="Not submitted">
        {error.message} Waiting:{" "}
        {claims.map((claim, i) => (
          <span key={claim}>
            {i > 0 && ", "}
            <Link to="/team/tests" className="font-mono underline">
              {claim}
            </Link>
          </span>
        ))}
        .
      </Notice>
    );
  }
  return <ErrorNotice error={error} title="Not submitted" />;
}

export function Submit() {
  const team = useTeam();
  const tests = useTests();
  const snapshots = useSnapshots();
  const submission = useSubmission();
  const now = useNow(team.data?.now);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  // One key per visit to this page: a double click, or a retry after a lost answer, is the same
  // submission. A new key is made only after a submission succeeds.
  const [key, setKey] = useState(newKey);
  const [chosen, setChosen] = useState<string | null>(null);
  const [declaration, setDeclaration] = useState<ReuseDeclaration>({
    starter_code: false,
    earlier_work: false,
    ai_assistance: false,
    notes: "",
  });
  const submit = useMutation({
    mutationFn: (body: SubmissionIn) => send<Receipt>("POST", "/team/submissions", body),
    onSuccess: () => {
      setKey(newKey());
      void queryClient.invalidateQueries({ queryKey: keys.submission });
      void queryClient.invalidateQueries({ queryKey: keys.team });
      navigate("/team/receipt");
    },
  });

  if (!team.data || tests.isPending || snapshots.isPending) return <Spinner label="Loading" />;
  if (tests.isError) return <ErrorNotice error={tests.error} />;
  if (snapshots.isError) return <ErrorNotice error={snapshots.error} />;

  const view = tests.data;
  const list = snapshots.data.snapshots;
  const selected = chosen ?? list[0]?.id ?? null;
  const drafted = view.drafting === "done";
  const waiting = view.claims.filter((claim) => claim.state === "draft").length;
  const over = isPast(team.data.event.deadline_at, now);
  const needs = {
    claims: team.data.progress.has_claims,
    tests: drafted && waiting === 0,
    snapshot: list.length > 0,
  };
  const ready = needs.claims && needs.tests && needs.snapshot && selected !== null && !over;

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!selected) return;
    submit.mutate({ snapshot_id: selected, mode: "sandbox", reuse_declaration: declaration, idempotency_key: key });
  }

  const flag = (field: "starter_code" | "earlier_work" | "ai_assistance", label: string) => (
    <label className="flex items-start gap-2 text-sm">
      <input
        type="checkbox"
        className="mt-0.5"
        checked={declaration[field]}
        onChange={(event) => setDeclaration({ ...declaration, [field]: event.target.checked })}
      />
      {label}
    </label>
  );

  return (
    <form onSubmit={onSubmit} className="space-y-6">
      <div className="space-y-2">
        <h1 className="text-xl font-semibold">Submit</h1>
        <p className="text-sm text-neutral-600">
          {over
            ? `The deadline passed ${when(team.data.event.deadline_at)}.`
            : `The deadline is ${when(team.data.event.deadline_at)}, in ${remaining(team.data.event.deadline_at, now)}.`}
        </p>
      </div>
      {submission.data && (
        <Notice tone="info" title="You have already submitted">
          Commit <Mono>{submission.data.receipt.commit_sha.slice(0, 10)}</Mono> was submitted{" "}
          {when(submission.data.receipt.submitted_at)}. Submitting again before the deadline replaces it as the one
          judged; the earlier one is kept, unchanged.
        </Notice>
      )}
      {over && <Notice tone="warn">Submissions are closed.</Notice>}

      <Section title="What a submission needs">
        <ul className="space-y-1.5">
          <Need met={needs.claims}>
            Your claims are saved (<Link to="/team/claims" className="underline">claims</Link>)
          </Need>
          <Need met={needs.tests}>
            {view.drafting === "failed"
              ? "Your tests are drafted (drafting failed: ask for them again)"
              : !drafted
                ? "Your tests are drafted (still being drafted)"
                : waiting > 0
                ? `Every drafted test is approved (${waiting} waiting)`
                : "Every drafted test is approved"}{" "}
            (<Link to="/team/tests" className="underline">tests</Link>)
          </Need>
          <Need met={needs.snapshot}>
            A snapshot of your code (<Link to="/team/tests" className="underline">self-check</Link>)
          </Need>
        </ul>
        <p className="text-xs text-neutral-500">
          A claim that still needs clarification, or whose browser test arrives later, does not stop you: it is judged
          unverified, with its reason.
        </p>
      </Section>

      <Section title="The snapshot to submit">
        {list.length === 0 ? (
          <p className="text-sm text-neutral-500">No snapshot yet.</p>
        ) : (
          <fieldset className="space-y-2">
            <legend className="sr-only">Snapshot</legend>
            {list.map((snapshot, index) => (
              <label key={snapshot.id} className="flex items-center gap-3 text-sm">
                <input
                  type="radio"
                  name="snapshot"
                  value={snapshot.id}
                  checked={selected === snapshot.id}
                  onChange={() => setChosen(snapshot.id)}
                />
                <Mono>{snapshot.commit_sha}</Mono>
                <span className="text-neutral-500">
                  taken {when(snapshot.created_at)}
                  {index === 0 && " · newest"}
                </span>
              </label>
            ))}
          </fieldset>
        )}
      </Section>

      <Section title="What you did not write during the event">
        <p className="text-sm text-neutral-600">Judges read this next to your code. Tick what applies.</p>
        <div className="space-y-2">
          {flag("starter_code", "We started from starter code, a template or a boilerplate.")}
          {flag("earlier_work", "We used work we had made before the event.")}
          {flag("ai_assistance", "We used AI assistance to write code.")}
        </div>
        <TextArea
          label="Notes (optional)"
          hint="Say what, and where it came from."
          value={declaration.notes}
          onChange={(notes) => setDeclaration({ ...declaration, notes })}
          max={NOTES_MAX}
          rows={3}
        />
      </Section>

      <div className="space-y-3">
        {submit.isError && <Refusal error={submit.error} />}
        <Button type="submit" busy={submit.isPending} disabled={!ready || declaration.notes.length > NOTES_MAX}>
          Submit
        </Button>
      </div>
    </form>
  );
}
