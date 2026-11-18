"""Tests des conversions de format des backends, avec des réponses HTTP simulées."""

import json

import pytest

from drone_agent_mandate.llm import LLMError, make_llm
from drone_agent_mandate.llm import ollama_client, openai_client
from drone_agent_mandate.llm.ollama_client import OllamaClient
from drone_agent_mandate.llm.openai_client import OpenAICompatibleClient

HISTORY = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "mission"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "takeoff", "arguments": '{"altitude_m": 10}'}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "takeoff", "content": "ok"},
]


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


def fake_post(captured, data):
    def post(url, json=None, **kwargs):
        captured.update(url=url, payload=json, kwargs=kwargs)
        return FakeResponse(data)
    return post


def test_ollama_conversion(monkeypatch):
    captured = {}
    monkeypatch.setattr(ollama_client.requests, "post", fake_post(captured, {
        "message": {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "goto", "arguments": {"lat": 1.5, "lon": 2, "altitude_m": 10}}}]}}))
    resp = OllamaClient(num_ctx=4096).chat(HISTORY, tools=[])

    sent = captured["payload"]
    assert captured["url"].endswith("/api/chat")
    assert sent["options"]["num_ctx"] == 4096 and sent["stream"] is False
    # arguments : chaîne JSON en interne -> objet chez Ollama ; résultat d'outil identifié par son nom
    assert sent["messages"][2]["tool_calls"][0]["function"]["arguments"] == {"altitude_m": 10}
    assert sent["messages"][3] == {"role": "tool", "content": "ok", "tool_name": "takeoff"}
    assert resp.tool_calls[0].name == "goto" and resp.tool_calls[0].arguments["lat"] == 1.5
    assert resp.tool_calls[0].id  # un identifiant est généré s'il manque


def test_openai_conversion(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    captured = {}
    monkeypatch.setattr(openai_client.requests, "post", fake_post(captured, {
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "land", "arguments": "{}"}},
            {"id": "c3", "type": "function", "function": {"name": "goto", "arguments": "{lat: 1"}}]}}]}))
    resp = OpenAICompatibleClient(model="m", api_key_env="TEST_KEY").chat(HISTORY, tools=[])

    assert captured["url"].endswith("/chat/completions")
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["payload"]["messages"] == HISTORY  # format interne = format OpenAI
    assert resp.content == ""
    assert resp.tool_calls[0].name == "land" and resp.tool_calls[0].parse_error is None
    assert resp.tool_calls[1].parse_error  # JSON invalide détecté, pas d'exception
    # Le texte brut est conservé dans l'historique renvoyé au modèle.
    assert resp.to_message()["tool_calls"][1]["function"]["arguments"] == "{lat: 1"


def test_openai_requires_env_key(monkeypatch):
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    with pytest.raises(LLMError):
        OpenAICompatibleClient(model="m", api_key_env="ABSENT_KEY")


def test_http_error_becomes_llm_error(monkeypatch):
    monkeypatch.setattr(ollama_client.requests, "post", lambda *a, **k: FakeResponse({"error": "model not found"}, 404))
    with pytest.raises(LLMError, match="404"):
        OllamaClient().chat(HISTORY, tools=[])


def test_make_llm_from_config():
    llm = make_llm({"backend": "ollama", "model": "qwen2.5:7b", "temperature": 0, "seed": 1,
                    "ollama": {"base_url": "http://x:1", "num_ctx": 2048}})
    assert isinstance(llm, OllamaClient) and llm.options == {"temperature": 0, "num_ctx": 2048, "num_predict": 512, "seed": 1}
    with pytest.raises(ValueError):
        make_llm({"backend": "inconnu", "model": "m"})
