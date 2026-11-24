"""생각(thinking) 끄기 — 요청마다 enable_thinking=false 를 붙인다."""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from cvtool.clients import llm as L
from cvtool.config import settings


@pytest.fixture
def 생각끔(monkeypatch):
    monkeypatch.setattr(L, "settings", dataclasses.replace(settings, llm_thinking=False))


def _클라(답, 본것, 거절=False):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        본것.append(body)
        if 거절 and "chat_template_kwargs" in body:
            return httpx.Response(400, text="Unknown field: chat_template_kwargs")
        return httpx.Response(200, json={"choices": [{"message": {"content": 답},
                                                       "finish_reason": "stop"}]})
    return L.LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_기본은_그대로(monkeypatch):
    monkeypatch.setattr(L, "settings", dataclasses.replace(settings, llm_thinking=True))
    본것 = []
    _클라("안녕", 본것).chat_text([{"role": "user", "content": "x"}])
    assert "chat_template_kwargs" not in 본것[0]


def test_끄면_글과_JSON_모두_붙는다(생각끔):
    본것 = []
    c = _클라('{"a": 1}', 본것)
    c.chat_text([{"role": "user", "content": "x"}])
    assert c.chat_json([{"role": "user", "content": "x"}], {"type": "object"}) == {"a": 1}
    assert all(b["chat_template_kwargs"] == {"enable_thinking": False} for b in 본것)


def test_서버가_모르면_빼고_다시(생각끔):
    본것 = []
    c = _클라('{"a": 1}', 본것, 거절=True)
    assert c.chat_json([{"role": "user", "content": "x"}], {"type": "object"}) == {"a": 1}
    assert c.chat_text([{"role": "user", "content": "x"}]) == '{"a": 1}'
    assert "chat_template_kwargs" in 본것[0]
    assert all("chat_template_kwargs" not in b for b in 본것[1:])   # 한 번 거절되면 이후엔 안 붙임
    assert 본것[1].get("guided_json") is not None                       # guided_json 은 그대로


def test_운영_설정에서_켜고_끈다():
    try:
        settings.바꾸기({"llm_thinking": "0"})
        assert settings.llm_thinking is False
    finally:
        settings._덮어씀.clear()
