import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/** The frame of the judge's and the organiser's pages: narrow, phone first. */
export function Frame({ title, home, children, wide = false }: { title: string; home: string; children: ReactNode; wide?: boolean }) {
  const width = wide ? "max-w-5xl" : "max-w-2xl";
  return (
    <div className="min-h-screen">
      <header className="hp-bar text-white">
        <div className={`mx-auto ${width} flex items-center justify-between gap-2 px-4 py-4`}>
          <Link to={home} className="inline-flex items-center gap-2.5 text-lg font-bold tracking-tight">
            <span className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-white/15 ring-1 ring-white/30">
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6l7-3z" />
                <path d="M9 12l2 2 4-4" />
              </svg>
            </span>
            HackProof
          </Link>
          <span className="rounded-full bg-white/15 px-3 py-1 text-xs font-semibold uppercase tracking-wider ring-1 ring-white/25">
            {title}
          </span>
        </div>
      </header>
      <main className={`mx-auto ${width} space-y-6 px-4 py-8`}>{children}</main>
    </div>
  );
}
