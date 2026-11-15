// What the judge's and the organiser's routes return (api/routers/judge.py, organiser.py).

import type { UiPage, UiStepResult } from "@/components/UiEvidence";
import type { Verdict } from "@/types";

export type JobState = "none" | "pending" | "done" | "failed";
export type BriefState = "ok" | "failed" | "pending" | "none";
export type Mark = "verified_live" | "not_verified";

export type Criterion = { id: string; name: string; description: string; max: number };

export type Assertion = {
  op: string;
  path: string | null;
  expected: unknown;
  actual: unknown;
  passed: boolean;
  note: string | null;
  step: string | null;
};

export type Case = {
  result_id: string;
  kind?: "ui";
  steps?: UiStepResult[];
  page?: UiPage | null;
  media?: { video: boolean; trace: boolean };
  case_id: string | null;
  verdict: Verdict;
  error: string | null;
  duration_ms: number | null;
  input: unknown;
  expected: unknown;
  request: { method: string; path: string; query: Record<string, string>; json: unknown } | null;
  response: { status?: number; body?: unknown; text?: string } | null;
  assertions_total: number;
  failed: Assertion[];
};

export type Citation = {
  id: string;
  path: string;
  start_line: number;
  end_line: number;
  why: string;
  url: string | null;
};

export type CodeMap = {
  method: "graph" | "search" | "none";
  anchors: { path: string; line: number; end_line: number; label: string }[];
  neighbours: { label: string; path?: string; line?: number; relation?: string; explicit?: boolean }[];
  hits: { path: string; line: number; text?: string }[];
};

export type BriefClaim = {
  id: string;
  text: string;
  target: string;
  type: string | null;
  verdict: Verdict;
  cases: Case[];
  audit: { implementation: string; explanation: string; citations: Citation[] } | null;
  code_map: CodeMap | null;
};

export type Question = { question: string; ref: { kind: "test_result" | "citation"; id: string } | null };

export type Signal = { kind: string; detail?: string; observed_at?: string };

export type Brief = {
  submission: {
    id: string;
    team_id: string;
    team_name: string;
    mode: string;
    commit_sha: string;
    archive_sha256: string;
    commit_url: string | null;
    submitted_at: string;
    receipt_hash: string;
  };
  run: {
    status: JobState;
    error: string | null;
    build_status: string | null;
    startup_status: string | null;
    startup_detail: string | null;
    image_digest: string | null;
    model_use?: {
      calls: number;
      calls_ok: number;
      refused: { reason: string; count: number }[];
      models: Record<string, number>;
      outside_hosts: Record<string, number>;
      outside_refused: number;
      receipt_verified: boolean | null;
    } | null;
  };
  claims: BriefClaim[];
  audit: {
    status: "ok" | "partial" | "failed" | "pending" | "none";
    failure: string | null;
    text_hints: { path: string; line: number; kind: string; excerpt: string }[];
    citations_dropped: number;
  };
  observations: {
    kind: string;
    observed_at: string;
    head_sha: string | null;
    previous_head_sha: string | null;
    lines_added: number | null;
    lines_total: number | null;
    descends_from_previous: boolean | null;
  }[];
  growth: { observed_at: string; lines: number }[];
  kickoff_at: string;
  signals: Signal[];
  reuse_declaration: Record<string, unknown>;
  written: {
    summary: string | null;
    claims: Record<string, string>;
    demo_questions: Question[];
    questions_from: "model" | "template";
  };
  ai_status: BriefState;
  rubric: Criterion[];
  scores_locked: boolean;
  can_score: boolean;
  own: { marks: Record<string, Mark>; scores: Record<string, number>; total: number };
};

export type JudgeTeams = {
  event: { id: string; name: string; scores_locked: boolean };
  criteria: number;
  teams: {
    team_id: string;
    team_name: string;
    submission: {
      id: string;
      submitted_at: string;
      verdicts: Record<string, Verdict>;
      brief: BriefState;
      scored: number;
      total: number;
    } | null;
  }[];
};

export type EventSummary = {
  id: string;
  name: string;
  kickoff_at: string;
  deadline_at: string;
  scores_locked: boolean;
};

export type Dashboard = {
  event: EventSummary & { rubric: Criterion[] };
  teams: {
    id: string;
    name: string;
    repo_url: string | null;
    registered: boolean;
    rules_acknowledged: boolean;
    claims_version: number | null;
    last_observed_at: string | null;
    last_polled_at: string | null;
    submission: {
      id: string;
      submitted_at: string;
      final_run: JobState;
      audit: JobState;
      brief: BriefState;
      verdicts: Record<string, Verdict>;
    } | null;
  }[];
  judges: { id: string; name: string; email: string | null; briefs_opened: number; submissions_scored: number }[];
  links: { id: string; role: "team" | "judge"; subject_id: string; subject_name: string; expires_at: string; revoked: boolean }[];
  jobs: {
    id: string;
    kind: string;
    status: string;
    team_name: string | null;
    attempts: number;
    error_class: string | null;
    error_detail: string | null;
    created_at: string;
    finished_at: string | null;
  }[];
};

export type SpotCheck = {
  team_id: string;
  team_name: string;
  claims_version: number | null;
  drafting: string;
  claims: {
    claim_id: string;
    text: string;
    state: string;
    test: {
      id: string;
      claim_type: string;
      version: number;
      status: string;
      reason: string | null;
      steps: Record<string, unknown>[];
      practice_cases: Record<string, unknown>[];
      final_cases: Record<string, unknown>[];
    } | null;
  }[];
};
