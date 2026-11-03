#!/usr/bin/env python3
"""Ask-all hub: ask every configured AI the same question, compare answers.

One question goes to each participant (ChatGPT, Claude, Gemini, and an
optional local Ollama model). Answers print as they arrive and are saved
as a Markdown transcript. A participant that fails never stops the rest.

Keys come only from the environment or a .env file next to this script.
Run --demo to watch mock participants answer with no keys and no network.

Usage:
    python3 hub.py --ask "What is the best way to learn penetration testing?"
    python3 hub.py --demo --ask "Why is the sky blue?"
    python3 hub.py --list
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone

from providers import (Answer, AnthropicClient, GeminiClient, MockClient,
                       OllamaClient, OpenAIClient, redact)

# (display name, client kind, model env var, key env var, default model)
PARTICIPANT_SPECS = [
    ("ChatGPT", "openai", "OPENAI_MODEL", "OPENAI_API_KEY", "gpt-4o-mini"),
    ("Claude", "anthropic", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY",
     "claude-sonnet-5-5"),
    ("Gemini", "gemini", "GEMINI_MODEL", "GEMINI_API_KEY", "gemini-3.8-flash"),
]

DEMO_REPLIES = {
    "ChatGPT": "Mock answer from ChatGPT to '{q}': start with networking "
               "basics, then work through hands-on labs, and finish with a "
               "structured course like a pentest certification path.",
    "Claude": "Mock answer from Claude to '{q}': learn TCP/IP and Linux "
              "first, then practice on legal lab targets, and document "
              "everything you find as if you were writing a client report.",
    "Gemini": "Mock answer from Gemini to '{q}': combine video courses "
              "with daily practice on vulnerable machines, and join a "
              "community so you can compare notes with other learners.",
    "Local (Ollama)": "Mock answer from the local model to '{q}': build a "
                      "home lab with virtual machines, break things safely, "
                      "and keep a notebook of every technique that worked.",
}

OLLAMA_ENABLED_VALUES = {"1", "true", "yes", "on"}


def load_dotenv(path=".env"):
    """Load KEY=VALUE lines into os.environ without overriding real env."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def build_participants(env=None, demo=False, timeout=60):
    """Return the configured participant clients.

    In demo mode, mock clients stand in for every participant. Otherwise a
    cloud participant is included only when its API key is set, and the
    local Ollama participant only when OLLAMA_ENABLED is truthy.
    """
    env = os.environ if env is None else env
    if demo:
        return [MockClient(name, reply=DEMO_REPLIES[name])
                for name in DEMO_REPLIES]
    clients = {"openai": OpenAIClient, "anthropic": AnthropicClient,
               "gemini": GeminiClient}
    participants = []
    for _name, kind, model_var, key_var, default_model in PARTICIPANT_SPECS:
        key = env.get(key_var, "").strip()
        if not key:
            continue
        model = env.get(model_var, "").strip() or default_model
        participants.append(clients[kind](key, model=model, timeout=timeout))
    if env.get("OLLAMA_ENABLED", "").strip().lower() in OLLAMA_ENABLED_VALUES:
        participants.append(OllamaClient(
            model=env.get("OLLAMA_MODEL", "").strip() or "qwen2.5:7b",
            timeout=timeout,
            host=env.get("OLLAMA_HOST", "").strip() or "http://localhost:11434"))
    return participants


def ask_all(participants, question):
    """Ask each participant; a failure is recorded, never fatal."""
    answers = []
    for client in participants:
        try:
            answers.append(client.ask(question))
        except Exception as exc:  # noqa: BLE001 - one bad client stops nothing
            answers.append(Answer(client.name, getattr(client, "model", "?"),
                                  error=f"unexpected: {redact(str(exc))}"))
    return answers


def render_transcript(question, answers, asked_at=None):
    asked_at = asked_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["# Ask-all comparison", "", f"Asked: {asked_at}", "",
             f"Question: {question}", ""]
    for answer in answers:
        lines.append(f"## {answer.name} (`{answer.model}`)")
        lines.append("")
        if answer.error:
            lines.append(f"_Error: {answer.error}_")
        else:
            lines.append(answer.text)
        lines.append("")
        lines.append(f"- time: {answer.seconds}s")
        if answer.usage:
            lines.append(f"- usage: {answer.usage}")
        lines.append("")
    return "\n".join(lines)


def save_transcript(save_dir, question, text):
    os.makedirs(save_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:40] or "ask"
    path = os.path.join(save_dir, f"{stamp}-{slug}.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def describe_participants(participants, env=None):
    env = os.environ if env is None else env
    lines = []
    for name, _kind, _model_var, key_var, _default in PARTICIPANT_SPECS:
        state = "key set" if env.get(key_var, "").strip() else "no key"
        lines.append(f"- {name}: {state}")
    ollama = ("enabled" if env.get("OLLAMA_ENABLED", "").strip().lower()
              in OLLAMA_ENABLED_VALUES else "disabled")
    lines.append(f"- Local (Ollama): {ollama}")
    if participants:
        lines.append("")
        lines.append("Configured now: " + ", ".join(p.name for p in participants))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Ask every configured AI the same question and compare.")
    parser.add_argument("question", nargs="?", help="question to ask")
    parser.add_argument("--ask", dest="ask", default=None,
                        help="question to ask (same as the positional arg)")
    parser.add_argument("--demo", action="store_true",
                        help="use mock participants: no keys, no network")
    parser.add_argument("--list", action="store_true",
                        help="show which participants are configured and exit")
    parser.add_argument("--env", default=".env",
                        help="path to the .env file (default: .env)")
    parser.add_argument("--save-dir", default="transcripts",
                        help="where to save transcripts (default: transcripts)")
    parser.add_argument("--no-save", action="store_true",
                        help="do not save a transcript file")
    parser.add_argument("--timeout", type=int, default=60,
                        help="seconds to wait per participant (default: 60)")
    args = parser.parse_args(argv)

    load_dotenv(args.env)
    participants = build_participants(timeout=args.timeout, demo=args.demo)

    if args.list:
        print(describe_participants(participants))
        return 0

    question = (args.ask or args.question or "").strip()
    if not question:
        parser.error("give a question, e.g. hub.py --ask \"...\"")
    if not participants:
        print("No participants configured.", file=sys.stderr)
        print("Set an API key in .env (see .env.example), enable Ollama with "
              "OLLAMA_ENABLED=true, or run with --demo to try mock answers.",
              file=sys.stderr)
        return 2

    answers = ask_all(participants, question)
    for answer in answers:
        print(f"\n===== {answer.name} (`{answer.model}`) =====")
        if answer.error:
            print(f"Error: {answer.error}")
        else:
            print(answer.text)
        print(f"[{answer.seconds}s" + (f", usage: {answer.usage}]" if answer.usage else "]"))

    if not args.no_save:
        path = save_transcript(args.save_dir, question,
                               render_transcript(question, answers))
        print(f"\nTranscript saved: {path}")

    return 1 if all(a.error for a in answers) else 0


if __name__ == "__main__":
    sys.exit(main())
