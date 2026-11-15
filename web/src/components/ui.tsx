import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, TextareaHTMLAttributes } from "react";
import { useId } from "react";
import { ApiError } from "@/api";
import type { ClaimState, Verdict } from "@/types";

function cx(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}

// --- buttons -----------------------------------------------------------------------------------

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger";
  busy?: boolean;
};

export function Button({ variant = "primary", busy = false, className, children, disabled, ...rest }: ButtonProps) {
  return (
    <button
      type="button"
      {...rest}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-md px-3.5 py-2 text-sm font-medium",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600",
        "disabled:cursor-not-allowed disabled:opacity-50",
        variant === "primary" && "bg-indigo-600 text-white hover:bg-indigo-500",
        variant === "secondary" && "border border-neutral-300 bg-white text-neutral-800 hover:bg-neutral-50",
        variant === "danger" && "border border-red-300 bg-white text-red-700 hover:bg-red-50",
        className,
      )}
    >
      {busy && <Spinner small />}
      {children}
    </button>
  );
}

// --- fields ------------------------------------------------------------------------------------

function Counter({ length, max }: { length: number; max: number }) {
  const over = length > max;
  return (
    <span className={cx("text-xs tabular-nums", over ? "font-medium text-red-700" : "text-neutral-500")}>
      {length.toLocaleString()} / {max.toLocaleString()}
    </span>
  );
}

type FieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "onChange"> & {
  label: string;
  hint?: ReactNode;
  value: string;
  onChange: (value: string) => void;
  max?: number;
};

export function Field({ label, hint, value, onChange, max, className, ...rest }: FieldProps) {
  const id = useId();
  return (
    <div className={className}>
      <div className="flex items-baseline justify-between gap-3">
        <label htmlFor={id} className="text-sm font-medium text-neutral-800">
          {label}
        </label>
        {max !== undefined && <Counter length={value.length} max={max} />}
      </div>
      <input
        id={id}
        {...rest}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 block w-full rounded-md border border-neutral-300 px-3 py-2 text-sm shadow-xs focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-neutral-100"
      />
      {hint && <p className="mt-1 text-xs text-neutral-500">{hint}</p>}
    </div>
  );
}

type TextAreaProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "onChange"> & {
  label: string;
  hint?: ReactNode;
  value: string;
  onChange: (value: string) => void;
  max?: number;
};

export function TextArea({ label, hint, value, onChange, max, className, rows = 4, ...rest }: TextAreaProps) {
  const id = useId();
  return (
    <div className={className}>
      <div className="flex items-baseline justify-between gap-3">
        <label htmlFor={id} className="text-sm font-medium text-neutral-800">
          {label}
        </label>
        {max !== undefined && <Counter length={value.length} max={max} />}
      </div>
      <textarea
        id={id}
        rows={rows}
        {...rest}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 block w-full rounded-md border border-neutral-300 px-3 py-2 text-sm shadow-xs focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-neutral-100"
      />
      {hint && <p className="mt-1 text-xs text-neutral-500">{hint}</p>}
    </div>
  );
}

// --- notices and badges --------------------------------------------------------------------------

type Tone = "info" | "warn" | "error" | "ok";

const TONES: Record<Tone, string> = {
  info: "border-sky-200 bg-sky-50 text-sky-900",
  warn: "border-amber-200 bg-amber-50 text-amber-900",
  error: "border-red-200 bg-red-50 text-red-900",
  ok: "border-emerald-200 bg-emerald-50 text-emerald-900",
};

export function Notice({ tone = "info", title, children }: { tone?: Tone; title?: string; children?: ReactNode }) {
  return (
    <div role={tone === "error" ? "alert" : "status"} className={cx("rounded-md border px-4 py-3 text-sm", TONES[tone])}>
      {title && <p className="font-medium">{title}</p>}
      {children && <div className={cx(title && "mt-1")}>{children}</div>}
    </div>
  );
}

export function ErrorNotice({ error, title }: { error: unknown; title?: string }) {
  if (!error) return null;
  const message =
    error instanceof ApiError ? error.message : error instanceof Error ? error.message : "Something went wrong.";
  return (
    <Notice tone="error" title={title}>
      {message}
    </Notice>
  );
}

const BADGES: Record<ClaimState | Verdict, { label: string; tone: string }> = {
  drafting: { label: "Being drafted", tone: "bg-sky-100 text-sky-800" },
  draft: { label: "Waiting for approval", tone: "bg-amber-100 text-amber-800" },
  approved: { label: "Approved", tone: "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-300" },
  needs_clarification: { label: "Needs clarification", tone: "bg-orange-100 text-orange-800" },
  ui_pending: { label: "Browser test later", tone: "bg-neutral-100 text-neutral-700" },
  drafting_failed: { label: "Drafting failed", tone: "bg-red-100 text-red-800" },
  pass: { label: "✓ Pass", tone: "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-300" },
  fail: { label: "✕ Fail", tone: "bg-rose-100 text-rose-800 ring-1 ring-rose-300" },
  blocked: { label: "Did not start", tone: "bg-neutral-200 text-neutral-800" },
  unverified: { label: "Unverified", tone: "bg-neutral-100 text-neutral-700" },
};

export function StateBadge({ state }: { state: ClaimState | Verdict }) {
  const badge = BADGES[state];
  return (
    <span className={cx("inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold", badge.tone)}>
      {badge.label}
    </span>
  );
}

export function Spinner({ small = false, label }: { small?: boolean; label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-neutral-600">
      <span
        aria-hidden="true"
        className={cx(
          "inline-block animate-spin rounded-full border-2 border-current border-r-transparent",
          small ? "h-3.5 w-3.5" : "h-4 w-4",
        )}
      />
      {label ? <span>{label}</span> : <span className="sr-only">Loading</span>}
    </span>
  );
}

export function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="relative overflow-hidden rounded-2xl border border-slate-200/80 bg-white/90 p-6 shadow-[0_8px_30px_-12px_rgb(30_27_75/0.18)] backdrop-blur">
      <div className="absolute inset-x-0 top-0 h-1 bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500" />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight text-slate-900">
          <span className="h-2 w-2 rounded-full bg-gradient-to-br from-indigo-500 to-violet-500" />
          {title}
        </h2>
        {aside}
      </div>
      <div className="mt-4 space-y-4">{children}</div>
    </section>
  );
}

/** A neutral label: it states a fact and never says good or bad. */
export function Tag({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full border border-neutral-300 bg-white px-2.5 py-0.5 text-xs font-medium text-neutral-700">
      {children}
    </span>
  );
}

/** A long hash or SHA shown in full, wrapping, in a monospace face. */
export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <code className={cx("font-mono text-xs break-all text-neutral-800", className)}>{children}</code>;
}
