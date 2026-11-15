import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { send } from "@/api";
import { Button, ErrorNotice, Field } from "@/components/ui";

/** The organiser's sign-in (section 5.1, step 1). Teams and judges use their invitation links. */
export function Login() {
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const signIn = useMutation({
    mutationFn: () => send("POST", "/auth/organiser", { password }),
    onSuccess: () => navigate("/org", { replace: true }),
  });
  return (
    <main className="mx-auto max-w-sm px-4 py-16">
      <h1 className="text-2xl font-semibold">HackProof</h1>
      <p className="mt-1 text-sm text-neutral-600">Organiser sign-in. Teams and judges open their invitation links.</p>
      <form
        className="mt-6 space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          signIn.mutate();
        }}
      >
        <Field label="Password" type="password" autoComplete="current-password" value={password} onChange={setPassword} />
        <ErrorNotice error={signIn.error} />
        <Button type="submit" className="w-full" busy={signIn.isPending} disabled={!password}>
          Sign in
        </Button>
      </form>
    </main>
  );
}
