import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ErrorNotice, Mono, Notice, Section, Spinner } from "@/components/ui";
import { useSubmission } from "@/queries";
import { when } from "@/time";

const FINAL_RUN: Record<string, string> = {
  queued: "waiting to run",
  running: "running",
  done: "finished",
  failed: "could not run; the organiser can see why",
  cancelled: "cancelled",
  none: "not started",
};

export function Receipt() {
  const submission = useSubmission();
  if (submission.isPending) return <Spinner label="Loading your receipt" />;
  if (submission.isError) return <ErrorNotice error={submission.error} />;
  if (submission.data === null) {
    return (
      <Notice tone="info" title="No submission yet">
        <Link to="/team/submit" className="underline">
          Submit a snapshot
        </Link>{" "}
        before the deadline to get a receipt.
      </Notice>
    );
  }

  const { receipt, submissions_made, final_run } = submission.data;
  const rows: [string, ReactNode][] = [
    ["Submission ID", <Mono>{receipt.submission_id}</Mono>],
    ["Team", `${receipt.team_name} (${receipt.team_id})`],
    ["Commit", <Mono>{receipt.commit_sha}</Mono>],
    ["Archive SHA-256", <Mono>{receipt.archive_sha256}</Mono>],
    ["Claims hash", <Mono>{receipt.claims_hash}</Mono>],
    ["Test spec hash", <Mono>{receipt.test_spec_hash}</Mono>],
    ["Rubric version", String(receipt.rubric_version)],
    ["Submitted at", <span>{when(receipt.submitted_at)} <Mono className="text-neutral-500">({receipt.submitted_at})</Mono></span>],
  ];
  const pending = final_run === "queued" || final_run === "running";

  return (
    <>
      <h1 className="text-xl font-semibold">Your receipt</h1>
      <Section title="The submission that will be judged">
        <table className="w-full text-left text-sm">
          <tbody className="divide-y divide-neutral-200">
            {rows.map(([label, value]) => (
              <tr key={label}>
                <th scope="row" className="w-44 py-2 pr-4 align-top font-medium text-neutral-600">
                  {label}
                </th>
                <td className="py-2 align-top">{value}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="rounded-md border border-indigo-200 bg-indigo-50 px-4 py-3">
          <p className="text-xs uppercase tracking-wide text-indigo-800">Receipt hash</p>
          <Mono className="mt-1 block text-sm">{receipt.receipt_hash}</Mono>
        </div>
        <p className="text-xs text-neutral-600">
          The receipt hash is the SHA-256 of the canonical JSON of the fields above except the team name: the
          submission ID, team ID, commit, archive SHA-256, claims hash, test spec hash, rubric version and the
          submitted-at time as written in brackets. Keep it: it proves what you submitted and when.
        </p>
      </Section>
      <Section title="Status">
        <p className="text-sm">
          {submissions_made === 1 ? "1 submission made" : `${submissions_made} submissions made`}; this is the latest.
        </p>
        <p className="flex items-center gap-2 text-sm">
          Final run: {pending ? <Spinner small label={FINAL_RUN[final_run]} /> : (FINAL_RUN[final_run] ?? final_run)}
        </p>
        <p className="text-xs text-neutral-500">
          The final run uses the hidden cases. Its results go to the judges, not to the team.
        </p>
      </Section>
    </>
  );
}
