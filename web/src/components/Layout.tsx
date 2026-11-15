import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { isApiError } from "@/api";
import { ErrorNotice, Notice, Spinner } from "@/components/ui";
import { useTeam } from "@/queries";
import { isPast, remaining, useNow } from "@/time";
import type { TeamView } from "@/types";

type NavItem = { to: string; label: string; end?: boolean };

const NAV: NavItem[] = [
  { to: "/team", label: "Overview", end: true },
  { to: "/team/claims", label: "Claims" },
  { to: "/team/tests", label: "Tests" },
  { to: "/team/submit", label: "Submit" },
  { to: "/team/receipt", label: "Receipt" },
];
// Opens once the deadline has passed (M9).
const PRIOR_WORK: NavItem = { to: "/team/prior-work", label: "Prior work" };

function Clocks({ team }: { team: TeamView }) {
  const now = useNow(team.now);
  const lock = remaining(team.event.claims_lock_at, now);
  const deadline = remaining(team.event.deadline_at, now);
  return (
    <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
      <span>
        <span className="text-neutral-500">Claims lock: </span>
        <span className="font-medium tabular-nums">{lock === "passed" ? "locked" : `in ${lock}`}</span>
      </span>
      <span>
        <span className="text-neutral-500">Deadline: </span>
        <span className="font-medium tabular-nums">{deadline === "passed" ? "passed" : `in ${deadline}`}</span>
      </span>
    </div>
  );
}

export function SignedOut() {
  return (
    <main className="mx-auto max-w-xl px-4 py-16">
      <h1 className="text-2xl font-semibold">HackProof</h1>
      <div className="mt-6">
        <Notice tone="info" title="You are not signed in">
          Open your invitation link to sign in. Your organiser sent each team its own link.
        </Notice>
      </div>
    </main>
  );
}

/** The frame of every team page: who is signed in, the navigation, and the event's clocks. */
export function Layout({ children }: { children: ReactNode }) {
  const team = useTeam();
  const now = useNow(team.data?.now);

  if (team.isPending) {
    return (
      <main className="mx-auto max-w-4xl px-4 py-16">
        <Spinner label="Loading" />
      </main>
    );
  }
  if (team.isError) {
    if (isApiError(team.error) && team.error.status === 401) return <SignedOut />;
    return (
      <main className="mx-auto max-w-4xl px-4 py-16">
        <ErrorNotice error={team.error} title="The team page could not be loaded" />
      </main>
    );
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-indigo-100 bg-white/80 backdrop-blur">
        <div className="hp-bar h-1.5" />
        <div className="mx-auto max-w-4xl px-4 pt-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <p className="text-xl font-bold tracking-tight text-neutral-900">
              {team.data.name}
              <span className="ml-2 text-sm font-normal text-neutral-500">{team.data.event.name}</span>
            </p>
            <Clocks team={team.data} />
          </div>
          <nav aria-label="Team pages" className="-mb-px mt-3 flex gap-1 overflow-x-auto">
            {[...NAV, ...(isPast(team.data.event.deadline_at, now) ? [PRIOR_WORK] : [])].map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium ${
                    isActive
                      ? "border-indigo-600 text-indigo-700"
                      : "border-transparent text-neutral-600 hover:border-neutral-300 hover:text-neutral-800"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-4xl space-y-6 px-4 py-6">{children}</main>
    </div>
  );
}
