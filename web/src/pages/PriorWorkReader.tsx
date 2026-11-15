// A submission's prior-work report as a judge or the organiser reads it (M9): the same report
// the team reads after the deadline, with the team's explanations, read only. Judges read it at
// once; it is context for questions, never a score.

import { useInfiniteQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { get } from "@/api";
import { notReadySentence, ReportBody } from "@/components/PriorWorkReport";
import { ErrorNotice, Notice, Spinner } from "@/components/ui";
import { when } from "@/time";
import type { PriorWorkView } from "@/types";

const PAGE = 100;

export function PriorWorkReader({ home }: { home: "/judge" | "/org" }) {
  const { submissionId = "" } = useParams();
  const report = useInfiniteQuery({
    queryKey: ["prior-work-reader", submissionId],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      get<PriorWorkView>(`/judge/submissions/${submissionId}/prior-work?offset=${pageParam}&limit=${PAGE}`),
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, page) => n + page.matches.length, 0);
      return loaded < last.matches_total ? loaded : undefined;
    },
    refetchInterval: (query) => (query.state.data?.pages[0]?.status === "pending" ? 5000 : false),
  });
  const back = (
    <Link to={`${home}/brief/${submissionId}`} className="text-sm text-indigo-700 underline">
      ← The brief
    </Link>
  );
  if (report.isPending) return <Spinner label="Loading the report" />;
  if (report.isError) return <ErrorNotice error={report.error} title="The report could not be loaded" />;
  const view = report.data.pages[0];
  if (!view) return <Spinner label="Loading the report" />;
  const sentence = notReadySentence(view, "the team");
  const matches = report.data.pages.flatMap((page) => page.matches);
  return (
    <div className="space-y-4">
      {back}
      <header>
        <h1 className="text-xl font-semibold text-neutral-900">{view.team_name}: prior work</h1>
        <p className="mt-1 text-sm text-neutral-600">
          Code that was already on the team's devices at kickoff, beside the final code. Context for your questions,
          not a verdict.
        </p>
      </header>
      {sentence ? (
        <Notice tone="info">{sentence}</Notice>
      ) : (
        <ReportBody
          view={view}
          matches={matches}
          whose="the team's"
          reader
          explanation={
            view.explanation ? (
              <p className="whitespace-pre-wrap text-sm text-neutral-700">
                <span className="font-medium">The team's word on the report</span>
                {view.explained_at && <span className="text-xs text-neutral-500"> ({when(view.explained_at)})</span>}:{" "}
                {view.explanation}
              </p>
            ) : (
              <p className="text-sm text-neutral-500">The team has not explained the report.</p>
            )
          }
          more={{ hasNext: report.hasNextPage, loading: report.isFetchingNextPage, load: () => void report.fetchNextPage() }}
        />
      )}
    </div>
  );
}
