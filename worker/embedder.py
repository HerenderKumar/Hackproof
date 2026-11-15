"""The "possibly related" list's model, inside the worker (M9 plan, Stage 5, Task 22).

`fast_embedder` is the factory the `prior_work` handler puts in its `CompareRequest` when
`EMBEDDINGS=on`. It runs in the `index` lane's pool process: the model is loaded once per
process and kept. The worker image downloads the model at build (`EMBED_CACHE_DIR`), and it is
read from there only, so device code goes to this in-process model and nowhere else: nothing is
sent to the model gateway or any other service.
"""

import os
from collections.abc import Sequence

from fastembed import TextEmbedding

from core.priorwork.related import Embedder

MODEL = "jinaai/jina-embeddings-v2-base-code"
BATCH = 16

_loaded: TextEmbedding | None = None


class FastEmbedder:
    def __init__(self, model: TextEmbedding) -> None:
        self._model = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(list(texts), batch_size=BATCH)]


def load(cache_dir: str | None = None, *, offline: bool = True) -> TextEmbedding:
    """The model from `cache_dir`. Offline, a missing model is an error, never a download."""
    return TextEmbedding(MODEL, cache_dir=cache_dir, local_files_only=offline)


def fast_embedder() -> Embedder:
    global _loaded
    if _loaded is None:
        _loaded = load(os.environ.get("EMBED_CACHE_DIR"))
    return FastEmbedder(_loaded)
