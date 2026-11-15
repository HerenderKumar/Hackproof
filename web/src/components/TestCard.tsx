import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { send } from "@/api";
import { Button, ErrorNotice, Spinner, StateBadge } from "@/components/ui";
import { keys } from "@/queries";
import { when } from "@/time";
import type { Assertion, ClaimTest, PracticeCase, Step, TestOut, TestsView, UiStepOut } from "@/types";

/** A value as the test states it: a template such as {{case.input.people}} stays as written. */
export function shown(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined) return "";
  return JSON.stringify(value);
}

function where(path: unknown): string {
  return typeof path === "string" && path ? path : "the body";
}

function within(tolerance: unknown): string {
  return typeof tolerance === "number" && tolerance > 0 ? ` (within ${tolerance})` : "";
}

/** One assertion in words: "status is 200", "$.shares has length {{case.input.people}}". */
export function inWords(assertion: Assertion): string {
  const { op, path, value, tolerance } = assertion;
  const v = shown(value);
  switch (op) {
    case "status_equals":
      return `status is ${v}`;
    case "equals":
      return `${where(path)} equals ${v}`;
    case "not_equals":
      return `${where(path)} is not ${v}`;
    case "approx_equals":
      return `${where(path)} is about ${v}${within(tolerance)}`;
    case "contains":
      return `${where(path)} contains ${v}`;
    case "exists":
      return `${where(path)} exists`;
    case "length_equals":
      return `${where(path)} has length ${v}`;
    case "gte":
      return `${where(path)} is at least ${v}`;
    case "lte":
      return `${where(path)} is at most ${v}`;
    case "matches":
      return `${where(path)} matches the pattern ${v}`;
    case "sum_equals":
      return `${where(path)} adds up to ${v}${within(tolerance)}`;
    case "header_contains":
      return `header ${shown(assertion.header)} contains ${v}`;
    case "body_contains":
      return `the body contains ${v}`;
    default:
      return `${op} ${path ?? ""} ${v}`.trim();
  }
}

function Json({ value }: { value: unknown }) {
  return (
    <pre className="overflow-x-auto rounded bg-neutral-100 px-3 py-2 font-mono text-xs text-neutral-800">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function StepView({ step, index }: { step: Step; index: number }) {
  const body = step.request.json;
  return (
    <li className="space-y-2">
      <p className="text-sm">
        <span className="text-neutral-500">Step {index + 1}: </span>
        <code className="font-mono font-medium">
          {step.request.method} {step.request.path}
        </code>
      </p>
      {body !== undefined && body !== null && <Json value={body} />}
      {step.assertions.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-sm text-neutral-800">
          {step.assertions.map((assertion, i) => (
            <li key={i}>
              <code className="font-mono text-xs">{inWords(assertion)}</code>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function UiStepList({ steps }: { steps: UiStepOut[] }) {
  return (
    <ol className="list-decimal space-y-1 pl-5 text-sm text-neutral-800">
      {steps.map((step, i) => (
        <li key={step.id ?? i}>{step.words ?? step.action}</li>
      ))}
    </ol>
  );
}

function CaseView({ practice }: { practice: PracticeCase }) {
  const expected = practice.expected;
  const hasExpected = typeof expected === "object" && expected !== null && Object.keys(expected).length > 0;
  return (
    <div className="space-y-1">
      <p className="text-sm font-medium">Practice case {practice.id}</p>
      <p className="text-xs text-neutral-500">Input</p>
      <Json value={practice.input} />
      {hasExpected && (
        <>
          <p className="text-xs text-neutral-500">Expected</p>
          <Json value={expected} />
        </>
      )}
    </div>
  );
}

function Approve({ test }: { test: TestOut }) {
  const queryClient = useQueryClient();
  const approve = useMutation({
    mutationFn: () => send<TestsView>("POST", `/team/tests/${test.id}/approve`, { spec_hash: test.spec_hash }),
    onSuccess: (view) => {
      queryClient.setQueryData(keys.tests, view);
      void queryClient.invalidateQueries({ queryKey: keys.team });
    },
    onError: () => void queryClient.invalidateQueries({ queryKey: keys.tests }),
  });
  if (test.approved_at) {
    return <p className="text-sm font-medium text-emerald-800">Approved {when(test.approved_at)}</p>;
  }
  return (
    <div className="space-y-2">
      <Button onClick={() => approve.mutate()} busy={approve.isPending}>
        Approve this test
      </Button>
      <ErrorNotice error={approve.error} />
    </div>
  );
}

function Redraft() {
  const queryClient = useQueryClient();
  const redraft = useMutation({
    mutationFn: () => send<TestsView>("POST", "/team/tests/redraft"),
    onSuccess: (view) => queryClient.setQueryData(keys.tests, view),
  });
  return (
    <div className="space-y-2">
      <p className="text-sm text-neutral-700">The test could not be drafted. Ask for it again.</p>
      <Button variant="secondary" onClick={() => redraft.mutate()} busy={redraft.isPending}>
        Try again
      </Button>
      <ErrorNotice error={redraft.error} />
    </div>
  );
}

function Body({ claim }: { claim: ClaimTest }) {
  switch (claim.state) {
    case "drafting":
      return <Spinner label="Drafting this test from your claim…" />;
    case "drafting_failed":
      return <Redraft />;
    case "needs_clarification":
      return (
        <div className="space-y-2 text-sm">
          <p className="text-neutral-800">{claim.test?.reason ?? "The claim is too vague to test."}</p>
          <p className="text-neutral-600">
            <Link to="/team/claims" className="underline">
              Make the claim clearer
            </Link>{" "}
            before the claims lock. Left as it is, it is judged unverified, with this reason.
          </p>
        </div>
      );
    case "ui_pending":
      return (
        <p className="text-sm text-neutral-700">
          No browser test is drafted for this claim on this event. Show it to the judges in your demo.
        </p>
      );
    case "draft":
    case "approved": {
      const test = claim.test;
      if (!test) return null;
      return (
        <div className="space-y-4">
          <p className="text-xs text-neutral-500">
            Kind of claim: {test.claim_type.replace("_", " ")} · test version {test.version}
          </p>
          {test.kind === "ui" ? (
            <>
              <p className="text-sm text-neutral-700">
                A browser opens your app and does these steps, with a video of each run. Values in {"{{ }}"} come
                from the case below; the hidden cases use other values.
              </p>
              <UiStepList steps={test.steps as UiStepOut[]} />
            </>
          ) : (
            <ol className="space-y-3">
              {(test.steps as Step[]).map((step, i) => (
                <StepView key={step.id ?? i} step={step} index={i} />
              ))}
            </ol>
          )}
          {test.practice_cases.map((practice) => (
            <CaseView key={practice.id} practice={practice} />
          ))}
          <Approve test={test} />
        </div>
      );
    }
  }
}

export function TestCard({ claim }: { claim: ClaimTest }) {
  return (
    <article className="rounded-lg border border-neutral-200 bg-white p-5 shadow-xs">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <p className="text-sm text-neutral-900">
          <span className="mr-2 font-mono text-xs text-neutral-500">{claim.claim_id}</span>
          {claim.text}
        </p>
        <StateBadge state={claim.state} />
      </header>
      <div className="mt-4">
        <Body claim={claim} />
      </div>
    </article>
  );
}
