// One match of the prior-work report (M9): where the code is now, where it was on a device at
// kickoff, both shown as text when opened, and the team's word on it.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { send } from "@/api";
import { Button, ErrorNotice, Mono, Spinner, Tag, TextArea } from "@/components/ui";
import { keys, useMatchExcerpts } from "@/queries";
import { when } from "@/time";
import type { Excerpt, ExplanationOut, MatchKind, MatchOut } from "@/types";

const KIND: Record<MatchKind, string> = {
  identical_file: "Identical file",
  unchanged: "Unchanged",
  edited: "Edited",
  possibly_related: "Possibly related",
};

function lines(from: number, to: number): string {
  return from === to ? `line ${from}` : `lines ${from}–${to}`;
}

function Code({ title, excerpt }: { title: string; excerpt: Excerpt }) {
  const rows = excerpt.text.split("\n");
  const width = String(excerpt.first_line + rows.length - 1).length;
  return (
    <div className="min-w-0">
      <p className="mb-1 text-xs font-medium text-neutral-600">{title}</p>
      <pre className="max-h-96 overflow-auto rounded border border-neutral-200 bg-neutral-50 p-2 font-mono text-xs leading-5 text-neutral-800">
        {rows.map((row, index) => `${String(excerpt.first_line + index).padStart(width, " ")}  ${row}`).join("\n")}
      </pre>
    </div>
  );
}

export function MatchCard({
  match,
  canExplain,
  codeDeletedAt,
  reader = false,
}: {
  match: MatchOut;
  canExplain: boolean;
  codeDeletedAt: string | null;
  /** A judge or the organiser: the excerpts come from their route; the explanation is the team's. */
  reader?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const path = reader ? `/judge/prior-work/matches/${match.id}` : `/team/prior-work/matches/${match.id}`;
  const excerpts = useMatchExcerpts(match.id, path, open && !codeDeletedAt);
  const queryClient = useQueryClient();
  const [text, setText] = useState(match.explanation ?? "");
  const save = useMutation({
    mutationFn: () =>
      send<ExplanationOut>("PUT", "/team/prior-work/explanations", { match_key: match.match_key, text }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.priorWork }),
  });

  return (
    <article className="rounded-md border border-neutral-200 bg-white p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Tag>{KIND[match.kind]}</Tag>
        {match.late && <Tag>late scan</Tag>}
        {match.score !== null && <Tag>score {match.score.toFixed(2)}</Tag>}
        <span className="text-xs text-neutral-500">{match.tokens} tokens</span>
      </div>
      <dl className="mt-2 grid gap-1 text-sm sm:grid-cols-[auto_1fr] sm:gap-x-3">
        <dt className="text-neutral-500">Final</dt>
        <dd className="min-w-0">
          <Mono>{match.final_path}</Mono> <span className="text-neutral-500">{lines(match.final_start_line, match.final_end_line)}</span>
        </dd>
        <dt className="text-neutral-500">On {match.device_label}</dt>
        <dd className="min-w-0">
          <Mono>{match.old_path}</Mono> <span className="text-neutral-500">{lines(match.old_start_line, match.old_end_line)}</span>
        </dd>
      </dl>
      {match.also_in > 0 && (
        <p className="mt-1 text-xs text-neutral-500">
          The same code is also in {match.also_in} other old {match.also_in === 1 ? "file" : "files"}.
        </p>
      )}
      <div className="mt-2">
        {codeDeletedAt ? (
          <p className="text-sm text-neutral-600">The code was deleted on {when(codeDeletedAt)}.</p>
        ) : (
          <Button variant="secondary" onClick={() => setOpen(!open)}>
            {open ? "Hide the code" : "Show the code"}
          </Button>
        )}
      </div>
      {open && !codeDeletedAt && (
        <div className="mt-3">
          {excerpts.data ? (
            <div className="grid gap-3 md:grid-cols-2">
              <Code title="Final submission" excerpt={excerpts.data.final} />
              <Code title={`On ${match.device_label} at kickoff`} excerpt={excerpts.data.old} />
            </div>
          ) : excerpts.isError ? (
            <ErrorNotice error={excerpts.error} />
          ) : (
            <Spinner label="Loading the code" />
          )}
        </div>
      )}
      <div className="mt-3 space-y-2">
        {canExplain ? (
          <>
            <TextArea
              label="Your explanation"
              hint="Where this code came from: an earlier project of yours, a tutorial, a declared library."
              value={text}
              onChange={setText}
              max={2000}
              rows={2}
            />
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="secondary"
                onClick={() => save.mutate()}
                busy={save.isPending}
                disabled={text.length > 2000 || text === (match.explanation ?? "")}
              >
                Save
              </Button>
              {match.explained_at && <span className="text-xs text-neutral-500">Saved {when(match.explained_at)}</span>}
            </div>
            <ErrorNotice error={save.error} />
          </>
        ) : (
          match.explanation && (
            <p className="whitespace-pre-wrap text-sm text-neutral-700">
              <span className="font-medium">{reader ? "The team's explanation" : "Your explanation"}: </span>
              {match.explanation}
            </p>
          )
        )}
      </div>
    </article>
  );
}
