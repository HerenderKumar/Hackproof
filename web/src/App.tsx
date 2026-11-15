import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { Notice } from "@/components/ui";
import { Frame } from "@/judging/Frame";
import { Brief } from "@/pages/Brief";
import { Claims } from "@/pages/Claims";
import { JudgeHome } from "@/pages/JudgeHome";
import { Login } from "@/pages/Login";
import { OrgHome } from "@/pages/OrgHome";
import { PriorWork } from "@/pages/PriorWork";
import { PriorWorkReader } from "@/pages/PriorWorkReader";
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
      <Route path="/team/prior-work" element={<Layout><PriorWork /></Layout>} />
      <Route path="/login" element={<Login />} />
      <Route path="/org" element={<Frame title="Organiser" home="/org" wide><OrgHome /></Frame>} />
      <Route path="/org/brief/:submissionId" element={<Frame title="Brief" home="/org"><Brief /></Frame>} />
      <Route path="/judge" element={<Frame title="Judge" home="/judge"><JudgeHome /></Frame>} />
      <Route path="/judge/brief/:submissionId" element={<Frame title="Brief" home="/judge"><Brief /></Frame>} />
      <Route path="/judge/prior-work/:submissionId" element={<Frame title="Prior work" home="/judge"><PriorWorkReader home="/judge" /></Frame>} />
      <Route path="/org/prior-work/:submissionId" element={<Frame title="Prior work" home="/org" wide><PriorWorkReader home="/org" /></Frame>} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
