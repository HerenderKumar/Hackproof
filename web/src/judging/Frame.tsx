import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/** The frame of the judge's and the organiser's pages: narrow, phone first. */
export function Frame({ title, home, children, wide = false }: { title: string; home: string; children: ReactNode; wide?: boolean }) {
  const width = wide ? "max-w-5xl" : "max-w-2xl";
  return (
    <div className="min-h-screen bg-neutral-50">
      <header className="border-b border-neutral-200 bg-white">
        <div className={`mx-auto ${width} flex items-baseline justify-between gap-2 px-4 py-3`}>
          <Link to={home} className="text-lg font-semibold text-neutral-900">
            HackProof
          </Link>
          <span className="text-sm text-neutral-500">{title}</span>
        </div>
      </header>
      <main className={`mx-auto ${width} space-y-4 px-4 py-5`}>{children}</main>
    </div>
  );
}
