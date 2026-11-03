"""Provider clients for the ask-all hub. Standard library only.

Each client implements ask(question) -> Answer. API keys are passed in by
the caller (from the environment); they are never logged, stored, or
included in error messages here.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class Answer:
    name: str
    model: str
    text: str = ""
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0
    error: str = ""


class ProviderError(Exception):
    """A provider call failed. `status` is the HTTP status (0 = no response)."""

    def __init__(self, provider, status, message):
        self.provider = provider
        self.status = status
        # Redact here, not only at the HTTP layer, so no construction path
        # can carry a key into an answer, a transcript, or a log line.
        super().__init__(f"{provider}: HTTP {status}: {redact(message)}")


# Masks anything that looks like an API key before it can reach an error
# message, a transcript, or a log line.
_KEY_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{4}|AIza[A-Za-z0-9_\-]{4}|xai-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]*")


def redact(text):
    if not isinstance(text, str):
        return text
    return _KEY_RE.sub(r"\1...redacted", text)


def _post_json(url, headers, payload, timeout, provider):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise ProviderError(provider, exc.code, redact(body)) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(provider, 0, redact(str(exc.reason))) from exc


def _timed(name, model, func):
    started = time.monotonic()
    try:
        text, usage = func()
        return Answer(name, model, text=text, usage=usage,
                      seconds=round(time.monotonic() - started, 2))
    except ProviderError as exc:
        return Answer(name, model, error=str(exc),
                      seconds=round(time.monotonic() - started, 2))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return Answer(name, model, error=f"{name}: unexpected response shape: {exc}",
                      seconds=round(time.monotonic() - started, 2))


class OpenAIClient:
    """ChatGPT via the OpenAI Chat Completions API."""

    name = "ChatGPT"

    def __init__(self, api_key, model="gpt-4o-mini", timeout=60,
                 base_url="https://api.openai.com/v1"):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    def ask(self, question):
        def call():
            data = _post_json(
                f"{self.base_url}/chat/completions",
                {"Authorization": f"Bearer {self.api_key}"},
                {"model": self.model,
                 "messages": [{"role": "user", "content": question}],
                 "temperature": 0.7},
                self.timeout, self.name)
            text = data["choices"][0]["message"]["content"]
            return text, data.get("usage", {})

        return _timed(self.name, self.model, call)


class GenericChatClient(OpenAIClient):
    """An OpenAI-shaped chat API under a different base URL and name.

    Grok, DeepSeek, Mistral, and OpenRouter all accept the same
    POST {base}/chat/completions payload and choices[0].message.content
    reply shape as OpenAIClient. Only the display name and base URL
    differ, so this subclass exists to stop their answers being
    labelled 'ChatGPT'.
    """

    def __init__(self, name, api_key, model, base_url, timeout=60):
        super().__init__(api_key, model=model, timeout=timeout,
                         base_url=base_url)
        self.name = name


class AnthropicClient:
    """Claude via the Anthropic Messages API."""

    name = "Claude"

    def __init__(self, api_key, model="claude-sonnet-5-5", timeout=60,
                 base_url="https://api.anthropic.com"):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    def ask(self, question):
        def call():
            data = _post_json(
                f"{self.base_url}/v1/messages",
                {"x-api-key": self.api_key,
                 "anthropic-version": "2023-06-01"},
                {"model": self.model, "max_tokens": 1024,
                 "messages": [{"role": "user", "content": question}]},
                self.timeout, self.name)
            text = "".join(block.get("text", "") for block in data.get("content", [])
                           if block.get("type") == "text")
            return text, data.get("usage", {})

        return _timed(self.name, self.model, call)


class GeminiClient:
    """Gemini via Google's generateContent API (key sent as a header)."""

    name = "Gemini"

    def __init__(self, api_key, model="gemini-3.8-flash", timeout=60,
                 base_url="https://generativelanguage.googleapis.com"):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    def ask(self, question):
        def call():
            data = _post_json(
                f"{self.base_url}/v1beta/models/{self.model}:generateContent",
                {"x-goog-api-key": self.api_key},
                {"contents": [{"parts": [{"text": question}]}]},
                self.timeout, self.name)
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
            return text, data.get("usageMetadata", {})

        return _timed(self.name, self.model, call)


class OllamaClient:
    """A local model via Ollama. No key; nothing leaves the machine."""

    name = "Local (Ollama)"

    def __init__(self, model="qwen2.5:7b", timeout=120,
                 host="http://localhost:11434"):
        self.model = model
        self.timeout = timeout
        self.host = host.rstrip("/")

    def ask(self, question):
        def call():
            data = _post_json(
                f"{self.host}/api/chat",
                {},
                {"model": self.model, "stream": False,
                 "messages": [{"role": "user", "content": question}]},
                self.timeout, self.name)
            return data["message"]["content"], {}

        return _timed(self.name, self.model, call)


class MockClient:
    """Canned replies for --demo: no keys, no network, proves the loop.

    Give `replies` (a list) to return a different canned reply per call,
    e.g. round 1 answer, round 2 critique, judge verdict. Otherwise the
    single `reply` is used, with {q} replaced by the question.
    """

    def __init__(self, name, model="mock", reply="", replies=None):
        self.name = name
        self.model = model
        self.reply = reply or f"[{name} mock answer]"
        self.replies = replies
        self.calls = 0
        self.prompts = []

    def ask(self, question):
        self.prompts.append(question)
        if self.replies is not None:
            idx = min(self.calls, len(self.replies) - 1)
            self.calls += 1
            text = self.replies[idx]
        else:
            text = self.reply
        try:
            text = text.format(q=question)
        except (KeyError, IndexError):
            pass
        return Answer(self.name, self.model, text=text, seconds=0.0)
