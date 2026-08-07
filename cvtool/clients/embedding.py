"""TEI 임베딩 클라이언트 (KURE-v1, 1024차원).

JD↔CV 유사도용. 다음 슬라이스(매칭)에서 사용.
TEI 는 배치 크기 제한이 있어 32개씩 잘라 보낸다. (기존 RAG embedding.py 와 동일 제약)
"""

from __future__ import annotations

import httpx

from ..config import settings


class EmbeddingClient:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._url = f"{settings.embed_url.rstrip('/')}/embed"
        self._batch = settings.embed_batch
        self._client = client or httpx.Client(timeout=60)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            chunk = texts[i : i + self._batch]
            resp = self._client.post(self._url, json={"inputs": chunk})
            resp.raise_for_status()
            out.extend(resp.json())
        return out

    def close(self) -> None:
        self._client.close()
