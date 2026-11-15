"""The worker's two questions to Prune about a finished run (Prune design, B3).

`run_receipt` reads the signed story of a run (`GET /v1/runs/{run_id}/receipt`: its steps,
models, tools, hosts and fence decisions). `verify` asks Prune's own check
(`POST /v1/receipts/verify`) whether its Ed25519 signature holds. Checking it offline with the
public key was tried in Stage 0 (D-064): the published SDK signs an older receipt shape, and no
canonical form of the run receipt we could build verified, so Prune's endpoint decides and the
result is stored with how it was checked.
"""

from typing import Any

import httpx


class PruneClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=timeout_s,
            trust_env=False,
        )

    async def __aenter__(self) -> "PruneClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def run_receipt(self, run_id: str) -> dict[str, Any]:
        reply = await self._client.get(f"/v1/runs/{run_id}/receipt")
        reply.raise_for_status()
        body = reply.json()
        if not isinstance(body, dict):
            raise ValueError("a run receipt is a JSON object")
        return body

    async def verify(self, receipt: dict[str, Any]) -> bool:
        reply = await self._client.post("/v1/receipts/verify", json={"receipt": receipt})
        reply.raise_for_status()
        return bool(reply.json().get("valid"))
