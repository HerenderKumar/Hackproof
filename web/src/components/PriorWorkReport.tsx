// The body of a prior-work report (M9), shared by the team's page and the readers' (judges and
// the organiser). The numbers are context for questions, never a score, so no colour here says
// good or bad: the tags, the bars and the percentage use the neutral text and border colours.

import type { ReactNode } from "react";
import { MatchCard } from "@/components/MatchCard";
import { Button, Section, Tag } from "@/components/ui";
import { when } from "@/time";
import type { FileShare, MatchOut, PriorWorkView, Totals } from "@/types";

const DEVICE_STATE = {
  missing: "Not scanned",
  uploading: "Upload not finished",
  complete: "Scanned",
  incomplete: "Scanned, incomplete",
} as const;

function share(part: number, whole: number): string {
  return whole ? `${Math.round((100 * part) / whole)}%` : "0%";
}

export function Summary({ totals, whose }: { totals: Totals; whose: string }) {
  return (
    <div className="space-y-2 text-sm text-neutral-800">
      {totals.percent === null ? (
        <p>There was no code to compare after leaving out starter and common code.</p>
      ) : (
        <p className="text-base">
          <span className="font-semibold tabular-nums">{totals.percent}%</span> of the final code matched code on{" "}
          {whose} devices at kickoff.
        </p>
      )}
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {(
          [
            ["Identical files", totals.identical],
            ["Unchanged", totals.unchanged],
            ["Edited", totals.edited],
            ["Starter code, left out", totals.excluded_starter],
            ["Common code, left out", totals.excluded_common],
          ] as const
        ).map(([label, value]) => (
          <div key={label} className="rounded-md border border-neutral-200 px-2 py-1.5">
            <dt className="text-xs text-neutral-500">{label}</dt>
            <dd className="font-medium tabular-nums">{value} tokens</dd>
          </div>
        ))}
      </dl>
      {totals.late_percent !== null && totals.late_percent > 0 && (
        <p>
          A further <span className="font-medium tabular-nums">{totals.late_percent}%</span> matched code from a scan made
          after kickoff. It is shown apart and is not part of the share above.
        </p>
      )}
      <p className="text-xs text-neutral-500">
        Counted over {totals.eligible} tokens of {totals.total}.
      </p>
    </div>
  );
}

function FileBar({ file }: { file: FileShare }) {
  const counted = file.total - file.excluded;
  const width = counted ? (100 * file.matched) / counted : 0;
  return (
    <li className="text-sm">
      <div className="flex justify-between gap-3">
        <span className="min-w-0 break-all font-mono text-xs text-neutral-800">{file.path}</span>
        <span className="shrink-0 tabular-nums text-xs text-neutral-600">{share(file.matched, counted)}</span>
      </div>
      <div className="mt-1 h-2 rounded bg-neutral-200" aria-hidden="true">
        <div className="h-2 rounded bg-neutral-500" style={{ width: `${width}%` }} />
      </div>
    </li>
  );
}

/** One plain sentence for a report that is not ready; null when it is. */
export function notReadySentence(view: PriorWorkView, whose: string): string | null {
  if (view.status === "pending") return "The report is being made. This page updates itself.";
  if (view.status === "no_baseline") return `No device of ${whose} was scanned at kickoff, so there is nothing to compare.`;
  if (view.status === "failed") return view.detail ? `The report could not be made: ${view.detail}.` : "The report could not be made.";
  return null;
}

export function ReportBody({
  view,
  matches,
  whose,
  reader,
  explanation,
  more,
}: {
  view: PriorWorkView;
  matches: MatchOut[];
  whose: string;
  reader: boolean;
  explanation: ReactNode;
  more: { hasNext: boolean; loading: boolean; load: () => void };
}) {
  const found = matches.filter((m) => m.kind !== "possibly_related");
  const related = matches.filter((m) => m.kind === "possibly_related");
  const card = (match: MatchOut) => (
    <MatchCard
      key={match.id}
      match={match}
      canExplain={view.can_explain}
      codeDeletedAt={view.code_deleted_at}
      reader={reader}
    />
  );
  return (
    <>
      <Section title="Prior work">
        {view.totals && <Summary totals={view.totals} whose={whose} />}
        {view.code_deleted_at && (
          <p className="text-sm text-neutral-600">The device code was deleted on {when(view.code_deleted_at)}.</p>
        )}
        {explanation}
      </Section>
      <Section title="Devices">
        {view.devices.length ? (
          <ul className="flex flex-wrap gap-x-4 gap-y-2">
            {view.devices.map((device) => (
              <li key={device.id} className="flex items-center gap-1.5 text-sm">
                {device.label} <Tag>{DEVICE_STATE[device.state]}</Tag>
                {device.late && <Tag>late</Tag>}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-neutral-600">No device.</p>
        )}
      </Section>
      <Section title="Files">
        <ul className="space-y-2">
          {view.files.map((file) => (
            <FileBar key={file.path} file={file} />
          ))}
        </ul>
      </Section>
      <Section title={`Matches (${view.matches_total})`}>
        {found.length > 0 ? <div className="space-y-3">{found.map(card)}</div> : <p className="text-sm">No match.</p>}
        {related.length > 0 && (
          <>
            <h3 className="pt-2 text-sm font-semibold text-neutral-900">Possibly related: check by eye</h3>
            <div className="space-y-3">{related.map(card)}</div>
          </>
        )}
        {more.hasNext && (
          <Button variant="secondary" onClick={more.load} busy={more.loading}>
            Show more
          </Button>
        )}
      </Section>
    </>
  );
}
