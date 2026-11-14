import { Route, Routes } from "react-router-dom";

function Placeholder() {
  return (
    <main className="mx-auto max-w-xl p-6">
      <h1 className="text-2xl font-semibold">HackProof</h1>
      <p className="mt-2 text-sm text-neutral-600">Scaffold only. Pages arrive in M7 and M10.</p>
    </main>
  );
}

export function App() {
  return (
    <Routes>
      <Route path="*" element={<Placeholder />} />
    </Routes>
  );
}
