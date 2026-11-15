import { StateBadge } from "@/components/ui";
import type { Verdict } from "@/types";

/** One chip per claim, in claim order. */
export function VerdictStrip({ verdicts }: { verdicts: Record<string, Verdict> }) {
  const ids = Object.keys(verdicts).sort();
  if (!ids.length) return <span className="text-sm text-neutral-500">No claims</span>;
  return (
    <span className="inline-flex flex-wrap gap-1.5">
      {ids.map((id) => (
        <span key={id} className="inline-flex items-center gap-1 text-xs text-neutral-600">
          <span className="font-mono">{id}</span>
          <StateBadge state={verdicts[id] ?? "unverified"} />
        </span>
      ))}
    </span>
  );
}

export function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}
