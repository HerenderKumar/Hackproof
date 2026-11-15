import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { send } from "@/api";
import { Button, ErrorNotice, Field, Notice, Section, Spinner, TextArea } from "@/components/ui";
import { keys, useClaims, useTeam } from "@/queries";
import { when } from "@/time";
import type { ClaimItemIn, ClaimsIn, ClaimsView } from "@/types";

// The API's limits (api/team_schemas.py), shown as counters.
const LIMITS = {
  problem_statement: 2000,
  explanation: 6000,
  text: 500,
  target: 200,
  example_input: 1000,
  expected_behaviour: 1000,
};
const MAX_CLAIMS = 3;

const blank = (): ClaimItemIn => ({ text: "", target: "", example_input: "", expected_behaviour: "" });

function ClaimFields({
  index,
  claim,
  onChange,
  onRemove,
  disabled,
}: {
  index: number;
  claim: ClaimItemIn;
  onChange: (claim: ClaimItemIn) => void;
  onRemove?: () => void;
  disabled: boolean;
}) {
  const set = (field: keyof ClaimItemIn) => (value: string) => onChange({ ...claim, [field]: value });
  return (
    <fieldset className="space-y-3 rounded-md border border-neutral-200 p-4">
      <div className="flex items-center justify-between">
        <legend className="text-sm font-semibold">Claim c{index + 1}</legend>
        {onRemove && !disabled && (
          <Button variant="danger" onClick={onRemove}>
            Remove
          </Button>
        )}
      </div>
      <TextArea
        label="What your app does"
        hint="One behaviour a judge can check, such as “Splits a bill so the shares add up to the total.”"
        value={claim.text}
        onChange={set("text")}
        max={LIMITS.text}
        rows={2}
        disabled={disabled}
        required
      />
      <Field
        label="Where (optional)"
        hint="The route or page, such as POST /split."
        value={claim.target}
        onChange={set("target")}
        max={LIMITS.target}
        disabled={disabled}
      />
      <div className="grid gap-3 sm:grid-cols-2">
        <TextArea
          label="An example input (optional)"
          value={claim.example_input}
          onChange={set("example_input")}
          max={LIMITS.example_input}
          rows={3}
          disabled={disabled}
        />
        <TextArea
          label="What should happen (optional)"
          value={claim.expected_behaviour}
          onChange={set("expected_behaviour")}
          max={LIMITS.expected_behaviour}
          rows={3}
          disabled={disabled}
        />
      </div>
    </fieldset>
  );
}

function ClaimsForm({ view, ready, onSaved }: { view: ClaimsView; ready: boolean; onSaved: (version: number) => void }) {
  const queryClient = useQueryClient();
  const current = view.current;
  const [problem, setProblem] = useState(current?.problem_statement ?? "");
  const [explanation, setExplanation] = useState(current?.explanation ?? "");
  const [items, setItems] = useState<ClaimItemIn[]>(
    current?.items.map(({ text, target, example_input, expected_behaviour }) => ({
      text,
      target,
      example_input,
      expected_behaviour,
    })) ?? [blank()],
  );
  const save = useMutation({
    mutationFn: (body: ClaimsIn) => send<ClaimsView>("PUT", "/team/claims", body),
    onSuccess: (saved) => {
      // The parent shows the confirmation: this form is re-made from the saved version.
      if (saved.current) onSaved(saved.current.version);
      queryClient.setQueryData(keys.claims, saved);
      void queryClient.invalidateQueries({ queryKey: keys.tests });
      void queryClient.invalidateQueries({ queryKey: keys.team });
    },
  });

  const locked = view.locked;
  const disabled = locked || !ready || save.isPending;
  const tooLong =
    problem.length > LIMITS.problem_statement ||
    explanation.length > LIMITS.explanation ||
    items.some(
      (item) =>
        item.text.length > LIMITS.text ||
        item.target.length > LIMITS.target ||
        item.example_input.length > LIMITS.example_input ||
        item.expected_behaviour.length > LIMITS.expected_behaviour,
    );
  const empty = !problem.trim() || !explanation.trim() || items.some((item) => !item.text.trim());

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate({ problem_statement: problem, explanation, items });
  }

  return (
    <form onSubmit={submit} className="space-y-6">
      <Section title="What you will build">
        <TextArea
          label="Problem statement"
          hint="The problem your team is solving, in a few sentences."
          value={problem}
          onChange={setProblem}
          max={LIMITS.problem_statement}
          rows={3}
          disabled={disabled}
          required
        />
        <TextArea
          label="Explanation"
          hint="How your app solves it: what it is, how it is used, what it answers."
          value={explanation}
          onChange={setExplanation}
          max={LIMITS.explanation}
          rows={6}
          disabled={disabled}
          required
        />
      </Section>
      <Section
        title="Claims"
        aside={<span className="text-xs text-neutral-500">{items.length} of {MAX_CLAIMS}</span>}
      >
        <p className="text-sm text-neutral-600">
          Each claim gets a test drafted from your words. You read and approve every test before you submit.
        </p>
        {items.map((item, index) => (
          <ClaimFields
            key={index}
            index={index}
            claim={item}
            disabled={disabled}
            onChange={(next) => setItems(items.map((old, i) => (i === index ? next : old)))}
            onRemove={items.length > 1 ? () => setItems(items.filter((_, i) => i !== index)) : undefined}
          />
        ))}
        {items.length < MAX_CLAIMS && !locked && (
          <Button variant="secondary" onClick={() => setItems([...items, blank()])} disabled={disabled}>
            Add a claim
          </Button>
        )}
      </Section>
      {!locked && (
        <div className="space-y-3">
          <ErrorNotice error={save.error} />
          <div className="flex flex-wrap items-center gap-4">
            <Button type="submit" busy={save.isPending} disabled={disabled || empty || tooLong || view.versions_left === 0}>
              Save and draft tests
            </Button>
            <span className="text-sm text-neutral-600">
              {view.versions_left === 1 ? "1 change left" : `${view.versions_left} changes left`} · locks{" "}
              {when(view.lock_at)}
            </span>
          </div>
        </div>
      )}
    </form>
  );
}

export function Claims() {
  const claims = useClaims();
  const team = useTeam();
  const [saved, setSaved] = useState<number | null>(null);
  if (claims.isPending) return <Spinner label="Loading your claims" />;
  if (claims.isError) return <ErrorNotice error={claims.error} />;
  const view = claims.data;
  const ready = Boolean(team.data?.rules_acknowledged_at);
  return (
    <>
      {view.locked && (
        <Notice tone="warn" title="Claims are locked">
          The claims lock passed {when(view.lock_at)}. Your claims and their tests are frozen; you can still approve
          drafted tests, self-check and submit until the deadline.
        </Notice>
      )}
      {!ready && (
        <Notice tone="info">
          <Link to="/team" className="underline">
            Acknowledge the event rules
          </Link>{" "}
          before writing your claims.
        </Notice>
      )}
      {view.current && (
        <p className="text-sm text-neutral-600">
          Version {view.current.version}, saved {when(view.current.created_at)}. Saving a change makes a new version
          and drafts every test again.
        </p>
      )}
      {saved !== null && saved === view.current?.version && (
        <Notice tone="ok" title={`Saved as version ${saved}`}>
          Your tests are being drafted.{" "}
          <Link to="/team/tests" className="underline">
            See them on the tests page
          </Link>
          .
        </Notice>
      )}
      {/* A new version re-fills the form from what was saved. */}
      <ClaimsForm key={view.current?.version ?? 0} view={view} ready={ready} onSaved={setSaved} />
    </>
  );
}
