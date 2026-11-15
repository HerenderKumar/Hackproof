import type { UiPage, UiStepResult } from "@/components/UiEvidence";
// The API's response bodies, field for field (api/schemas.py and api/team_schemas.py).

// --- the team overview -------------------------------------------------------------------------

export interface TeamEvent {
  id: string;
  name: string;
  kickoff_at: string;
  claims_lock_at: string;
  deadline_at: string;
  policy_text: string;
}

export interface TeamRepo {
  url: string;
  provider: string;
  default_branch: string;
  registered_at: string;
  late: boolean;
}

export interface TeamProgress {
  has_claims: boolean;
  tests_approved: number;
  tests_total: number;
  has_snapshot: boolean;
  has_submitted: boolean;
}

export interface TeamView {
  id: string;
  name: string;
  now: string;
  event: TeamEvent;
  rules_acknowledged_at: string | null;
  repo: TeamRepo | null;
  suggested_repo_url: string | null;
  health_path: string;
  progress: TeamProgress;
}

export interface RepoIn {
  repo_url: string;
  health_path?: string;
}

// --- claims ------------------------------------------------------------------------------------

export interface ClaimItemIn {
  text: string;
  target: string;
  example_input: string;
  expected_behaviour: string;
}

export interface ClaimItem extends ClaimItemIn {
  id: string;
}

export interface ClaimsIn {
  problem_statement: string;
  explanation: string;
  items: ClaimItemIn[];
}

export interface ClaimsOut {
  version: number;
  problem_statement: string;
  explanation: string;
  items: ClaimItem[];
  claims_hash: string;
  created_at: string;
}

export interface ClaimsView {
  current: ClaimsOut | null;
  lock_at: string;
  locked: boolean;
  versions_left: number;
}

// --- tests -------------------------------------------------------------------------------------

export type ClaimState =
  | "drafting"
  | "draft"
  | "approved"
  | "needs_clarification"
  | "ui_pending"
  | "drafting_failed";

export type Drafting = "none" | "queued" | "running" | "done" | "failed";

export interface Assertion {
  op: string;
  path?: string;
  value?: unknown;
  tolerance?: number;
  [key: string]: unknown;
}

export interface StepRequest {
  method: string;
  path: string;
  json?: unknown;
  [key: string]: unknown;
}

export interface Step {
  id?: string;
  request: StepRequest;
  assertions: Assertion[];
  [key: string]: unknown;
}

export interface PracticeCase {
  id: string;
  input: Record<string, unknown>;
  [key: string]: unknown;
}

export interface UiStepOut {
  id?: string;
  action: string;
  words: string | null;
  [key: string]: unknown;
}

export interface TestOut {
  id: string;
  kind: "api" | "ui";
  claim_type: string;
  version: number;
  spec_hash: string;
  steps: Step[] | UiStepOut[];
  practice_cases: PracticeCase[];
  reason: string | null;
  approved_at: string | null;
}

export interface ClaimTest {
  claim_id: string;
  text: string;
  state: ClaimState;
  test: TestOut | null;
}

export interface TestsView {
  claims_version: number | null;
  drafting: Drafting;
  claims: ClaimTest[];
}

// --- snapshots and self-checks ------------------------------------------------------------------

export interface SnapshotOut {
  id: string;
  commit_sha: string;
  created_at: string;
}

export interface PendingCommit {
  commit_sha: string;
  status: "queued" | "running" | "refused" | "failed";
  detail: string | null;
}

export interface SnapshotsView {
  snapshots: SnapshotOut[];
  pending: PendingCommit[];
}

export interface SnapshotRequest {
  commit_sha: string;
  status: "ready" | "queued" | "running";
  snapshot_id: string | null;
}

export interface SelfCheckStart {
  job_id: string;
  status: string;
}

export interface RunOut {
  build_status: string;
  startup_status: string;
  startup_detail: string | null;
}

export interface AssertionResult {
  op: string;
  path?: string | null;
  expected?: unknown;
  actual?: unknown;
  passed?: boolean;
  note?: string | null;
  step?: string | null;
  [key: string]: unknown;
}

export type Verdict = "pass" | "fail" | "blocked" | "unverified";

export interface ResultOut {
  result_id: string;
  kind: "api" | "ui";
  steps: UiStepResult[] | null;
  page: UiPage | null;
  has_video: boolean;
  has_trace: boolean;
  claim_id: string;
  case_id: string | null;
  verdict: Verdict;
  assertion_results: AssertionResult[] | null;
  error: string | null;
  duration_ms: number | null;
}

export interface SelfCheckView {
  job_id: string;
  snapshot_id: string | null;
  commit_sha: string | null;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  error: string | null;
  run: RunOut | null;
  results: ResultOut[];
  claim_verdicts: Record<string, Verdict>;
}

// --- submissions -------------------------------------------------------------------------------

export interface ReuseDeclaration {
  starter_code: boolean;
  earlier_work: boolean;
  ai_assistance: boolean;
  notes: string;
}

export interface SubmissionIn {
  snapshot_id: string;
  mode: "sandbox";
  reuse_declaration: ReuseDeclaration;
  idempotency_key: string;
}

export interface Receipt {
  submission_id: string;
  team_id: string;
  team_name: string;
  commit_sha: string;
  archive_sha256: string;
  claims_hash: string;
  test_spec_hash: string;
  rubric_version: number;
  submitted_at: string;
  receipt_hash: string;
}

export interface CurrentSubmission {
  receipt: Receipt;
  submissions_made: number;
  final_run: string;
}
