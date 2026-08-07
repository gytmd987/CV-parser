"""TEI 리랭커 클라이언트 (bge-reranker-v2-m3).

후보(CV)들을 JD 기준으로 재정렬. 다음 슬라이스(매칭)에서 사용.
"""

from __future__ import annotations

import httpx

from ..config import settings


class RerankerClient:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._url = f"{settings.rerank_url.rstrip('/')}/rerank"
        self._client = client or httpx.Client(timeout=60)

    def rerank(self, query: str, texts: list[str]) -> list[dict]:
        """[{index, score}, ...] 를 score 내림차순으로 반환."""
        if not texts:
            return []
        resp = self._client.post(self._url, json={"query": query, "texts": texts})
        resp.raise_for_status()
        results = resp.json()
        return sorted(results, key=lambda r: r.get("score", 0.0), reverse=True)

    def close(self) -> None:
        self._client.close()
