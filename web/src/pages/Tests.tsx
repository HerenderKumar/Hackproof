import { Link } from "react-router-dom";
import { SelfCheck } from "@/components/SelfCheck";
import { TestCard } from "@/components/TestCard";
import { ErrorNotice, Notice, Spinner } from "@/components/ui";
import { useTests } from "@/queries";

export function Tests() {
  const tests = useTests();
  if (tests.isPending) return <Spinner label="Loading your tests" />;
  if (tests.isError) return <ErrorNotice error={tests.error} />;
  const view = tests.data;

  if (view.claims_version === null) {
    return (
      <Notice tone="info" title="No tests yet">
        Tests are drafted from your claims.{" "}
        <Link to="/team/claims" className="underline">
          Write your claims
        </Link>{" "}
        first.
      </Notice>
    );
  }

  const waiting = view.claims.filter((claim) => claim.state === "draft").length;
  return (
    <>
      <div className="space-y-2">
        <h1 className="text-xl font-semibold">Your tests</h1>
        <p className="text-sm text-neutral-600">
          Drafted from claims version {view.claims_version}. Each test shows its steps and its practice case. Three
          more cases per test stay hidden until the final run, so a test cannot be passed by matching its examples.
        </p>
        {(view.drafting === "queued" || view.drafting === "running") && (
          <Spinner label="Drafting your tests. This page updates by itself." />
        )}
        {waiting > 0 && (
          <Notice tone="warn">
            {waiting === 1 ? "One test is" : `${waiting} tests are`} waiting for your approval. A submission needs every
            drafted test approved.
          </Notice>
        )}
      </div>
      <div className="space-y-4">
        {view.claims.map((claim) => (
          <TestCard key={claim.claim_id} claim={claim} />
        ))}
      </div>
      <SelfCheck tests={view} />
    </>
  );
}
