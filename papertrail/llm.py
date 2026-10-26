"""Nebius Token Factory client (OpenAI-compatible chat completions). Stdlib only.

All network goes through a `transport` callable so tests can replay recorded responses:
    transport(url: str, payload: dict, headers: dict) -> dict   # parsed JSON response body
`replay_transport(paths)` returns a transport that serves recorded bodies in order.
"""
import json
import os
import re
import urllib.request
from pathlib import Path

API_URL = os.environ.get("PAPERTRAIL_API_URL", "https://api.tokenfactory.nebius.com/v1/chat/completions")
OCR_MODEL = os.environ.get("PAPERTRAIL_OCR_MODEL", "Qwen/Qwen3.8-27B")
NEMOTRON = os.environ.get("PAPERTRAIL_NEMOTRON", "nvidia/nemotron-3-super-120b-a12b")
SECRETS_FILE = Path.home() / ".config" / "papertrail" / "secrets.env"


class LLMError(RuntimeError):
    pass


class LLMTruncated(LLMError):
    """finish_reason == "length": the model (often its hidden reasoning) used up max_tokens."""


def api_key():
    """NEBIUS_API_KEY from env, else from ~/.config/papertrail/secrets.env (outside the repo)."""
    key = os.environ.get("NEBIUS_API_KEY", "").strip()
    if not key and SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            if line.startswith("NEBIUS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise LLMError("NEBIUS_API_KEY is not set")
    return key


def http_transport(url, payload, headers, timeout=180):
    headers = {**headers, "Authorization": "Bearer " + api_key()}
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def replay_transport(paths):
    """Serve recorded response bodies (JSON files) in order; fail if more calls than recordings."""
    bodies = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    calls = []

    def transport(url, payload, headers):
        if len(calls) >= len(bodies):
            raise LLMError(f"replay exhausted after {len(bodies)} responses")
        calls.append(payload)
        return bodies[len(calls) - 1]

    transport.calls = calls
    return transport


def chat(model, messages, *, max_tokens=2000, temperature=0, transport=None):
    """Return the assistant message text for `messages` (OpenAI chat format)."""
    payload = {"model": model, "max_tokens": max_tokens, "temperature": temperature, "messages": messages}
    body = (transport or http_transport)(API_URL, payload, {"Content-Type": "application/json"})
    try:
        choice = body["choices"][0]
        content = (choice["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected response shape: {str(body)[:200]}") from exc
    if choice.get("finish_reason") == "length":
        raise LLMTruncated(f"{model} hit max_tokens={max_tokens}")
    return content


def parse_json(text):
    """Extract the first JSON object from model output (tolerates ```json fences and prose around it)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(candidate)
    except ValueError as exc:
        raise LLMError(f"model did not return JSON: {text[:200]}") from exc
