"""Optional language model. It may word the root-cause hypothesis; it never
chooses an action. Every claim it makes goes through diagnose.ground()."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


class ModelUnavailable(Exception):
    pass


@dataclass
class Answer:
    text: str
    tokens_in: int
    tokens_out: int


class Ollama:
    def __init__(self, model: str, host: Optional[str] = None, timeout: float = 60.0):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
        self.timeout = timeout
        self.name = f"ollama:{model}"

    def ask(self, prompt: str) -> Answer:
        body = json.dumps(
            {"model": self.model, "prompt": prompt, "stream": False, "format": "json", "options": {"temperature": 0}}
        ).encode()
        req = urllib.request.Request(
            f"{self.host}/api/generate", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ConnectionRefusedError):
                reason = "nothing is listening there"
            raise ModelUnavailable(f"Ollama at {self.host}: {reason}") from e
        return Answer(data.get("response", ""), int(data.get("prompt_eval_count", 0)), int(data.get("eval_count", 0)))


class Anthropic:
    def __init__(self, model: str, timeout: float = 60.0):
        self.model = model
        self.timeout = timeout
        self.name = f"anthropic:{model}"

    def ask(self, prompt: str) -> Answer:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ModelUnavailable("ANTHROPIC_API_KEY is not set")
        body = json.dumps(
            {"model": self.model, "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]}
        ).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ModelUnavailable(f"Anthropic API: {e}") from e
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        return Answer(text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))


def from_spec(spec: Optional[str]):
    """'none', 'ollama:<model>' or 'anthropic:<model>'."""
    if not spec or spec == "none":
        return None
    provider, _, model = spec.partition(":")
    if provider == "ollama" and model:
        return Ollama(model)
    if provider == "anthropic" and model:
        return Anthropic(model)
    raise ValueError(f"model spec {spec!r} must be none, ollama:<model> or anthropic:<model>")


def extract_json(text: str) -> Optional[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
