// A UI test's evidence (M11 design, part 8): the captioned video, the steps in words with how each
// ended, what the page had when a case did not pass, and the trace as a download. The sentences
// come from the API (core/ui_words.py); this file holds no wording of its own for a step.
import { useState } from "react";
import { Mono } from "@/components/ui";

export type UiStepResult = {
  id?: string;
  words?: string | null;
  status?: "pass" | "fail" | "error" | "skipped";
  expected?: unknown;
  actual?: unknown;
  message?: string;
};

export type UiPage = {
  outline?: string;
  refused?: string[];
  console_errors?: { text: string; url?: string | null; line?: number | null }[];
  page_errors?: string[];
  dialogs?: { type: string; message: string }[];
  popups?: string[];
};

type StepMark = { sign: string; tone: string; label: string };
const NOT_REACHED: StepMark = { sign: "–", tone: "text-neutral-400", label: "not reached" };
const MARKS: Record<string, StepMark> = {
  pass: { sign: "✓", tone: "text-emerald-700", label: "held" },
  fail: { sign: "✗", tone: "text-red-700", label: "did not hold" },
  error: { sign: "?", tone: "text-amber-700", label: "could not be checked" },
  skipped: NOT_REACHED,
};

function text(value: unknown): string {
  if (value === "") return "nothing (empty)";
  if (value === null || value === undefined) return "nothing that could be read";
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** The video, with its still image as the poster; nothing loads until it is played. */
export function CaseVideo({ base, hasVideo }: { base: string; hasVideo: boolean }) {
  if (!hasVideo) return <p className="text-xs text-neutral-500">No video was kept for this case.</p>;
  return (
    <video
      className="aspect-video w-full rounded-md border border-neutral-200 bg-neutral-900"
      controls
      playsInline
      preload="none"
      poster={`${base}/poster`}
      src={`${base}/video`}
    >
      <a href={`${base}/video`}>Download the video</a>
    </video>
  );
}

export function UiSteps({ steps }: { steps: UiStepResult[] }) {
  return (
    <ol className="space-y-1 text-sm">
      {steps.map((step, i) => {
        const mark = MARKS[step.status ?? "skipped"] ?? NOT_REACHED;
        return (
          <li key={step.id ?? i} className="flex gap-2">
            <span className={`w-4 shrink-0 text-center font-semibold ${mark.tone}`} aria-label={mark.label}>
              {mark.sign}
            </span>
            <div className={step.status === "skipped" ? "text-neutral-400" : "text-neutral-800"}>
              <p>{step.words ?? step.id}</p>
              {step.status === "fail" && "expected" in step && (
                <p className="text-xs text-red-800">
                  expected <Mono>{text(step.expected)}</Mono>, found <Mono>{text(step.actual)}</Mono>
                </p>
              )}
              {step.status === "error" && step.message && <p className="text-xs text-amber-800">{step.message}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** What the page held and did: for working out why a control was not found. */
export function PageDetails({ page, open = false }: { page: UiPage | null | undefined; open?: boolean }) {
  const [shown, setShown] = useState(open);
  if (!page) return null;
  const notes: string[] = [];
  if (page.refused?.length) {
    notes.push(`The page asked for files outside the app, which a run cannot reach: ${page.refused.join(", ")}`);
  }
  for (const error of page.page_errors ?? []) notes.push(`Page error: ${error}`);
  for (const error of page.console_errors ?? []) notes.push(`Console error: ${error.text}`);
  for (const dialog of page.dialogs ?? []) notes.push(`An ${dialog.type} said “${dialog.message}” and was accepted`);
  for (const popup of page.popups ?? []) notes.push(`A pop-up to ${popup} was closed`);
  return (
    <div className="space-y-2 text-xs">
      {notes.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-neutral-700">
          {notes.map((note, i) => (
            <li key={i} className="break-words">
              {note}
            </li>
          ))}
        </ul>
      )}
      {page.outline && (
        <div>
          <button type="button" className="font-medium text-indigo-700 underline" onClick={() => setShown(!shown)}>
            {shown ? "Hide" : "Show"} what the page had
          </button>
          {shown && (
            <pre className="mt-1 max-h-72 overflow-auto rounded bg-neutral-100 px-3 py-2 font-mono text-neutral-800">
              {page.outline}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

export function TraceLink({ base, hasTrace }: { base: string; hasTrace: boolean }) {
  if (!hasTrace) return null;
  return (
    <p className="text-xs text-neutral-600">
      <a className="font-medium text-indigo-700 underline" href={`${base}/trace`} download>
        Download the trace
      </a>{" "}
      and open it at{" "}
      <a className="underline" href="https://trace.playwright.dev" target="_blank" rel="noreferrer">
        trace.playwright.dev
      </a>
      , which reads the file in your browser and sends it nowhere.
    </p>
  );
}
