import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { Notice } from "@/components/ui";
import { Claims } from "@/pages/Claims";
import { Receipt } from "@/pages/Receipt";
import { Redeem } from "@/pages/Redeem";
import { Submit } from "@/pages/Submit";
import { TeamHome } from "@/pages/TeamHome";
import { Tests } from "@/pages/Tests";

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
      <Route path="/team/claims" element={<Layout><Claims /></Layout>} />
      <Route path="/team/tests" element={<Layout><Tests /></Layout>} />
      <Route path="/team/submit" element={<Layout><Submit /></Layout>} />
      <Route path="/team/receipt" element={<Layout><Receipt /></Layout>} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
