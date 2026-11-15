// The team's prior-work report (M9): opens after the deadline. How much of the final code was
// already on the team's devices at kickoff, file by file and match by match, each match with its
// old and new code and the team's explanation. The numbers are context, never a score, so no
// colour here says good or bad.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { send } from "@/api";
import { MatchCard } from "@/components/MatchCard";
import { Button, ErrorNotice, Notice, Section, Spinner, Tag, TextArea } from "@/components/ui";
import { keys, usePriorWork } from "@/queries";
import { when } from "@/time";
import type { ExplanationOut, FileShare, MatchOut, PriorWorkView, Totals } from "@/types";

const DEVICE_STATE = {
  missing: "Not scanned",
  uploading: "Upload not finished",
  complete: "Scanned",
  incomplete: "Scanned, incomplete",
} as const;

function share(part: number, whole: number): string {
  return whole ? `${Math.round((100 * part) / whole)}%` : "0%";
}

function Summary({ totals }: { totals: Totals }) {
  return (
    <div className="space-y-2 text-sm text-neutral-800">
      {totals.percent === null ? (
        <p>There was no code to compare after leaving out starter and common code.</p>
      ) : (
        <p className="text-base">
          <span className="font-semibold tabular-nums">{totals.percent}%</span> of the final code matched code on the
          team's devices at kickoff.
        </p>
      )}
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {[
          ["Identical files", totals.identical],
          ["Unchanged", totals.unchanged],
          ["Edited", totals.edited],
          ["Starter code, left out", totals.excluded_starter],
          ["Common code, left out", totals.excluded_common],
        ].map(([label, value]) => (
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
      <p className="text-xs text-neutral-500">Counted over {totals.eligible} tokens of {totals.total}.</p>
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

function WholeReport({ view }: { view: PriorWorkView }) {
  const queryClient = useQueryClient();
  const [text, setText] = useState(view.explanation ?? "");
  const save = useMutation({
    mutationFn: () => send<ExplanationOut>("PUT", "/team/prior-work/explanations", { match_key: "", text }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.priorWork }),
  });
  if (!view.can_explain) {
    return view.explanation ? (
      <p className="whitespace-pre-wrap text-sm text-neutral-700">
        <span className="font-medium">Your word on the report: </span>
        {view.explanation}
      </p>
    ) : null;
  }
  return (
    <div className="space-y-2">
      <TextArea
        label="About the whole report"
        hint="Anything the judges should know first, for example a library you declared."
        value={text}
        onChange={setText}
        max={2000}
        rows={3}
      />
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="secondary"
          onClick={() => save.mutate()}
          busy={save.isPending}
          disabled={text.length > 2000 || text === (view.explanation ?? "")}
        >
          Save
        </Button>
        {view.explained_at && <span className="text-xs text-neutral-500">Saved {when(view.explained_at)}</span>}
      </div>
      <ErrorNotice error={save.error} />
    </div>
  );
}

function Matches({ matches, view }: { matches: MatchOut[]; view: PriorWorkView }) {
  const found = matches.filter((m) => m.kind !== "possibly_related");
  const related = matches.filter((m) => m.kind === "possibly_related");
  const card = (match: MatchOut) => (
    <MatchCard key={match.id} match={match} canExplain={view.can_explain} codeDeletedAt={view.code_deleted_at} />
  );
  return (
    <>
      {found.length > 0 ? <div className="space-y-3">{found.map(card)}</div> : <p className="text-sm">No match.</p>}
      {related.length > 0 && (
        <>
          <h3 className="pt-2 text-sm font-semibold text-neutral-900">Possibly related: check by eye</h3>
          <div className="space-y-3">{related.map(card)}</div>
        </>
      )}
    </>
  );
}

export function PriorWork() {
  const report = usePriorWork();
  if (report.isPending) return <Spinner label="Loading" />;
  if (report.isError) return <ErrorNotice error={report.error} title="The report could not be loaded" />;
  const first = report.data.pages[0];
  if (first === undefined) return <Spinner label="Loading" />;
  if (first === "report_not_open") return <Notice tone="info">Your report opens after the deadline.</Notice>;
  if (first === "no_submission") return <Notice tone="info">Your team made no submission by the deadline.</Notice>;
  const view = first;
  if (view.status === "pending") return <Notice tone="info">Your report is being made. This page updates itself.</Notice>;
  if (view.status === "no_baseline") {
    return <Notice tone="info">No device of your team was scanned at kickoff, so there is nothing to compare.</Notice>;
  }
  if (view.status === "failed") return <Notice tone="info">The report could not be made.</Notice>;
  const matches = report.data.pages.flatMap((page) => (typeof page === "string" ? [] : page.matches));

  return (
    <>
      <Section title="Prior work">
        {view.totals && <Summary totals={view.totals} />}
        {view.code_deleted_at && (
          <p className="text-sm text-neutral-600">The device code was deleted on {when(view.code_deleted_at)}.</p>
        )}
        <WholeReport view={view} />
      </Section>
      <Section title="Devices">
        <ul className="flex flex-wrap gap-2">
          {view.devices.map((device) => (
            <li key={device.id} className="flex items-center gap-1.5 text-sm">
              {device.label} <Tag>{DEVICE_STATE[device.state]}</Tag>
              {device.late && <Tag>late</Tag>}
            </li>
          ))}
        </ul>
      </Section>
      <Section title="Files">
        <ul className="space-y-2">
          {view.files.map((file) => (
            <FileBar key={file.path} file={file} />
          ))}
        </ul>
      </Section>
      <Section title={`Matches (${view.matches_total})`}>
        <Matches matches={matches} view={view} />
        {report.hasNextPage && (
          <Button variant="secondary" onClick={() => report.fetchNextPage()} busy={report.isFetchingNextPage}>
            Show more
          </Button>
        )}
      </Section>
    </>
  );
}
