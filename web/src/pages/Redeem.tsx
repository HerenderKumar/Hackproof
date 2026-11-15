import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { Notice, Spinner } from "@/components/ui";

type Problem = { title: string; body: string };

const INVALID: Problem = {
  title: "This link is not valid",
  body: "Check that you opened the whole link your organiser sent, or ask them for a new one.",
};

const ERRORS: Record<string, Problem> = {
  invalid: INVALID,
  revoked: {
    title: "This link has been revoked",
    body: "Your organiser has replaced it. Ask them for the new link.",
  },
  expired: {
    title: "This link has expired",
    body: "Ask your organiser for a new link.",
  },
};

/** Opening an invitation link: the API checks the token, sets the session and sends us on. */
export function Redeem() {
  const [params] = useSearchParams();
  const token = params.get("token");
  const error = params.get("error");

  useEffect(() => {
    if (token && !error) {
      window.location.replace(`/api/auth/redeem?token=${encodeURIComponent(token)}`);
    }
  }, [token, error]);

  const problem = error
    ? (ERRORS[error] ?? INVALID)
    : token
      ? null
      : { title: "This link is incomplete", body: "Open the whole invitation link your organiser sent." };

  return (
    <main className="mx-auto max-w-xl px-4 py-16">
      <h1 className="text-2xl font-semibold">HackProof</h1>
      <div className="mt-6">
        {problem ? (
          <Notice tone="error" title={problem.title}>
            {problem.body}
          </Notice>
        ) : (
          <Spinner label="Signing you in" />
        )}
      </div>
    </main>
  );
}
