// The team's prior-work report (M9): opens after the deadline. How much of the final code was
// already on the team's devices at kickoff, file by file and match by match, each match with its
// old and new code and the team's explanation.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { send } from "@/api";
import { notReadySentence, ReportBody } from "@/components/PriorWorkReport";
import { Button, ErrorNotice, Notice, Spinner, TextArea } from "@/components/ui";
import { keys, usePriorWork } from "@/queries";
import { when } from "@/time";
import type { ExplanationOut, PriorWorkView } from "@/types";

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

export function PriorWork() {
  const report = usePriorWork();
  if (report.isPending) return <Spinner label="Loading" />;
  if (report.isError) return <ErrorNotice error={report.error} title="The report could not be loaded" />;
  const first = report.data.pages[0];
  if (first === undefined) return <Spinner label="Loading" />;
  if (first === "report_not_open") return <Notice tone="info">Your report opens after the deadline.</Notice>;
  if (first === "no_submission") return <Notice tone="info">Your team made no submission by the deadline.</Notice>;
  const sentence = notReadySentence(first, "your team");
  if (sentence) return <Notice tone="info">{sentence}</Notice>;
  const matches = report.data.pages.flatMap((page) => (typeof page === "string" ? [] : page.matches));
  return (
    <ReportBody
      view={first}
      matches={matches}
      whose="your team's"
      reader={false}
      explanation={<WholeReport view={first} />}
      more={{ hasNext: report.hasNextPage, loading: report.isFetchingNextPage, load: () => void report.fetchNextPage() }}
    />
  );
}
