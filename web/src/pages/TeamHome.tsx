import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { send } from "@/api";
import { KickoffScan } from "@/components/KickoffScan";
import { Button, ErrorNotice, Field, Mono, Notice, Section } from "@/components/ui";
import { keys, useTeam, useTests } from "@/queries";
import { isPast, remaining, useNow, when } from "@/time";
import type { RepoIn, TeamView, TestsView } from "@/types";

function nextStep(team: TeamView, now: Date, tests: TestsView | undefined): { text: string; to?: string } {
  const { progress } = team;
  if (isPast(team.event.deadline_at, now)) {
    return progress.has_submitted
      ? { text: "The event is over. Your receipt shows what you submitted.", to: "/team/receipt" }
      : { text: "The event is over." };
  }
  if (!team.rules_acknowledged_at) return { text: "Read the event rules below and acknowledge them." };
  if (!team.repo) return { text: "Register your team's public repository below." };
  if (!progress.has_claims) {
    return { text: "Write your problem statement and up to three claims.", to: "/team/claims" };
  }
  const drafting = tests?.drafting;
  if (drafting === undefined || drafting === "queued" || drafting === "running") {
    return { text: "Your tests are being drafted from your claims. Check the tests page.", to: "/team/tests" };
  }
  if (drafting === "failed") {
    return { text: "Drafting your tests failed. Ask for them again on the tests page.", to: "/team/tests" };
  }
  if (progress.tests_approved < progress.tests_total) {
    const left = progress.tests_total - progress.tests_approved;
    return { text: `Read and approve your drafted tests (${left} left).`, to: "/team/tests" };
  }
  if (!progress.has_snapshot) {
    return { text: "Snapshot a commit and run a self-check against your tests.", to: "/team/tests" };
  }
  if (!progress.has_submitted) return { text: "Submit a snapshot before the deadline.", to: "/team/submit" };
  return {
    text: "Submitted. You can submit again before the deadline; the latest one is judged.",
    to: "/team/receipt",
  };
}

function Times({ team, now }: { team: TeamView; now: Date }) {
  const rows = [
    { label: "Kickoff", at: team.event.kickoff_at },
    { label: "Claims lock", at: team.event.claims_lock_at },
    { label: "Deadline", at: team.event.deadline_at },
  ];
  return (
    <dl className="grid gap-3 sm:grid-cols-3">
      {rows.map((row) => {
        const left = remaining(row.at, now);
        return (
          <div key={row.label} className="rounded-md border border-neutral-200 px-3 py-2">
            <dt className="text-xs uppercase tracking-wide text-neutral-500">{row.label}</dt>
            <dd className="mt-1 text-sm font-medium">{when(row.at)}</dd>
            <dd className="text-xs tabular-nums text-neutral-600">{left === "passed" ? "passed" : `in ${left}`}</dd>
          </div>
        );
      })}
    </dl>
  );
}

function Rules({ team }: { team: TeamView }) {
  const queryClient = useQueryClient();
  const acknowledge = useMutation({
    mutationFn: () => send<TeamView>("POST", "/team/rules-ack"),
    onSuccess: (view) => queryClient.setQueryData(keys.team, view),
  });
  return (
    <Section title="Event rules">
      {team.event.policy_text ? (
        <p className="whitespace-pre-wrap text-sm text-neutral-800">{team.event.policy_text}</p>
      ) : (
        <p className="text-sm text-neutral-500">The organiser has not written any rules.</p>
      )}
      {team.rules_acknowledged_at ? (
        <p className="text-sm text-emerald-800">Acknowledged {when(team.rules_acknowledged_at)}.</p>
      ) : (
        <div className="space-y-2">
          <Button onClick={() => acknowledge.mutate()} busy={acknowledge.isPending}>
            I have read the rules
          </Button>
          <ErrorNotice error={acknowledge.error} />
        </div>
      )}
    </Section>
  );
}

/** What the app may reach during HackProof's runs, when the model door is on (Prune design). */
function ModelDoor({ team }: { team: TeamView }) {
  const door = team.model_door;
  if (!door) return null;
  return (
    <Section title="Models and outside calls during runs">
      <p className="text-sm text-neutral-700">
        During self-checks and the final run your app has no internet, except through HackProof's model door. It may
        call these models through Prune: <Mono>{door.models.join(", ")}</Mono>. Use the stock OpenAI or Anthropic
        SDK with no key of your own: HackProof sets <Mono>OPENAI_BASE_URL</Mono>, <Mono>OPENAI_API_KEY</Mono>,{" "}
        <Mono>ANTHROPIC_BASE_URL</Mono> and <Mono>ANTHROPIC_API_KEY</Mono> for each run, and lists the models in{" "}
        <Mono>HP_MODELS</Mono>.
      </p>
      <p className="text-sm text-neutral-700">
        Each run may spend up to ${door.run_budget_usd.toFixed(2)} in at most {door.max_calls} model calls. Prune
        refuses calls past that, and blocks calls that carry secrets or personal data or ask for a tool the event
        does not allow; the app gets the refusal as an error. Prune's cache and cheaper-model routing are off, so
        every call reaches the model you named.
      </p>
      {door.outside_hosts.length > 0 ? (
        <p className="text-sm text-neutral-700">
          Your app may also call these outside hosts over HTTPS: <Mono>{door.outside_hosts.join(", ")}</Mono>.
          HackProof sets <Mono>HTTPS_PROXY</Mono> for each run; anything else is refused.
        </p>
      ) : (
        <p className="text-sm text-neutral-700">No outside host is open in this event.</p>
      )}
      <p className="text-xs text-neutral-500">
        How many calls your app made is shown with each self-check and to the judges. It never changes a verdict.
      </p>
    </Section>
  );
}

function Repository({ team }: { team: TeamView }) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState(team.suggested_repo_url ?? "");
  const [healthPath, setHealthPath] = useState(team.health_path);
  const register = useMutation({
    mutationFn: (body: RepoIn) => send<TeamView>("POST", "/team/repo", body),
    onSuccess: (view) => queryClient.setQueryData(keys.team, view),
  });

  if (team.repo) {
    return (
      <Section title="Repository">
        <p className="text-sm">
          <a href={team.repo.url} className="font-medium text-indigo-700 underline" target="_blank" rel="noreferrer">
            {team.repo.url}
          </a>
          <span className="text-neutral-500"> · branch {team.repo.default_branch}</span>
        </p>
        <p className="text-sm text-neutral-600">
          Registered {when(team.repo.registered_at)}
          {team.repo.late && " (after kickoff: judges see this as context)"}. Health check path{" "}
          <code className="font-mono">{team.health_path}</code>.
        </p>
      </Section>
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const body: RepoIn = { repo_url: url.trim() };
    if (healthPath.trim() && healthPath.trim() !== team.health_path) body.health_path = healthPath.trim();
    register.mutate(body);
  }

  const blocked = !team.rules_acknowledged_at;
  return (
    <Section title="Repository">
      {blocked && <Notice tone="info">Acknowledge the rules first.</Notice>}
      <form onSubmit={submit} className="space-y-3">
        <Field
          label="Public repository URL"
          value={url}
          onChange={setUrl}
          placeholder="https://github.com/your-team/your-project"
          disabled={blocked}
          required
        />
        <Field
          label="Health check path"
          hint="The path HackProof requests to see that your app has started."
          value={healthPath}
          onChange={setHealthPath}
          disabled={blocked}
        />
        <ErrorNotice error={register.error} />
        <Button type="submit" busy={register.isPending} disabled={blocked || !url.trim()}>
          Register repository
        </Button>
      </form>
    </Section>
  );
}

export function TeamHome() {
  const team = useTeam();
  const tests = useTests();
  const now = useNow(team.data?.now);
  if (!team.data) return null; // the Layout shows loading and errors
  const step = nextStep(team.data, now, tests.data);
  return (
    <>
      <Notice tone="info" title="Next step">
        {step.to ? (
          <Link to={step.to} className="underline">
            {step.text}
          </Link>
        ) : (
          step.text
        )}
      </Notice>
      <Section title="The event">
        <Times team={team.data} now={now} />
        <p className="text-sm text-neutral-600">
          Claims and their tests can change until the claims lock. Self-checks and submissions go on until the
          deadline.
        </p>
      </Section>
      <Rules team={team.data} />
      <Repository team={team.data} />
      <ModelDoor team={team.data} />
      <KickoffScan serverNow={team.data.now} />
    </>
  );
}
