import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { Notice } from "@/components/ui";
import { Redeem } from "@/pages/Redeem";
import { TeamHome } from "@/pages/TeamHome";

function Later({ title }: { title: string }) {
  return (
    <Notice tone="info" title={title}>
      This page is not built yet.
    </Notice>
  );
}

function NotFound() {
  return (
    <main className="mx-auto max-w-xl px-4 py-16">
      <h1 className="text-2xl font-semibold">HackProof</h1>
      <div className="mt-6">
        <Notice tone="warn" title="Page not found">
          There is nothing at this address. Teams start from <a className="underline" href="/team">their page</a>.
        </Notice>
      </div>
    </main>
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/team" replace />} />
      <Route path="/redeem" element={<Redeem />} />
      <Route path="/team" element={<Layout><TeamHome /></Layout>} />
      <Route path="/team/claims" element={<Layout><Later title="Claims" /></Layout>} />
      <Route path="/team/tests" element={<Layout><Later title="Tests" /></Layout>} />
      <Route path="/team/submit" element={<Layout><Later title="Submit" /></Layout>} />
      <Route path="/team/receipt" element={<Layout><Later title="Receipt" /></Layout>} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
