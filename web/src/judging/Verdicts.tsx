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
          <span className="font-medium text-slate-500">{id.replace(/^c(\d+)$/, "Claim $1")}</span>
          <StateBadge state={verdicts[id] ?? "unverified"} />
        </span>
      ))}
    </span>
  );
}

/** A value in words a person reads: "total: 250.5 · people: 4", lists comma-separated. */
export function readable(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") {
    const t = value.trim();
    if ((t.startsWith("{") && t.endsWith("}")) || (t.startsWith("[") && t.endsWith("]"))) {
      try {
        return readable(JSON.parse(t));
      } catch {
        return value;
      }
    }
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    return value.every((v) => v === null || typeof v !== "object") ? value.map(readable).join(", ") : value.map(readable).join("; ");
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) return "—";
    return entries
      .map(([k, v]) => `${k}: ${v !== null && typeof v === "object" && !Array.isArray(v) ? `(${readable(v)})` : readable(v)}`)
      .join(" · ");
  }
  return String(value);
}

export function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}
