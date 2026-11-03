#!/usr/bin/env python3
"""Ask-all hub: ask every configured AI the same question, compare answers.

One question goes to each participant (ChatGPT, Claude, Gemini, Grok,
DeepSeek, Mistral, OpenRouter, and an optional local Ollama model).
Answers print as they arrive and are saved as a Markdown transcript.
A participant that fails never stops the rest.

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

from providers import (Answer, AnthropicClient, GeminiClient,
                       GenericChatClient, MockClient, OllamaClient,
                       OpenAIClient, redact)

# (display name, client kind, model env var, key env var, default model,
#  base URL; None except for "generic" clients, which share the
#  OpenAI-shaped chat API under their own base URL)
PARTICIPANT_SPECS = [
    ("ChatGPT", "openai", "OPENAI_MODEL", "OPENAI_API_KEY", "gpt-4o-mini",
     None),
    ("Claude", "anthropic", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY",
     "claude-sonnet-5-5", None),
    ("Gemini", "gemini", "GEMINI_MODEL", "GEMINI_API_KEY", "gemini-3.8-flash",
     None),
    ("Grok", "generic", "XAI_MODEL", "XAI_API_KEY", "grok-4-1-fast",
     "https://api.x.ai/v1"),
    ("DeepSeek", "generic", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY",
     "deepseek-flash", "https://api.deepseek.com/v1"),
    ("Mistral", "generic", "MISTRAL_MODEL", "MISTRAL_API_KEY",
     "mistral-small-latest", "https://api.mistral.ai/v1"),
    ("OpenRouter", "generic", "OPENROUTER_MODEL", "OPENROUTER_API_KEY",
     "openrouter/auto", "https://openrouter.ai/api/v1"),
]

DEMO_SCRIPTS = {
    # Each mock answers round 1, critiques in round 2, and (for the judge)
    # delivers a verdict in round 3.
    "ChatGPT": [
        "Mock answer from ChatGPT to '{q}': start with networking basics, "
        "then work through hands-on labs, and finish with a structured "
        "course like a pentest certification path.",
        "Mock critique from ChatGPT: the other answers skip structured "
        "courses, which matter for interviews. My improved answer keeps "
        "the labs-first order but adds a certification milestone.",
        "WINNER: Claude\n\nMock verdict: the most complete learning path.\n\n"
        "Final approved answer: learn TCP/IP and Linux, practice on legal "
        "lab targets, document everything, and finish with a certification.",
    ],
    "Claude": [
        "Mock answer from Claude to '{q}': learn TCP/IP and Linux first, "
        "then practice on legal lab targets, and document everything you "
        "find as if you were writing a client report.",
        "Mock critique from Claude: the other answers underplay reporting, "
        "which is half the job. My improved answer adds a report template "
        "to every lab exercise.",
        "WINNER: Claude\n\nMock verdict: reporting is what turns practice "
        "into employable skill.\n\nFinal approved answer: learn TCP/IP and "
        "Linux, practice on legal lab targets, and write a client-style "
        "report for every finding.",
    ],
    "Gemini": [
        "Mock answer from Gemini to '{q}': combine video courses with daily "
        "practice on vulnerable machines, and join a community so you can "
        "compare notes with other learners.",
        "Mock critique from Gemini: the other answers are solo-only; a "
        "community catches bad habits early. My improved answer adds a "
        "weekly peer review to the practice routine.",
        "WINNER: Claude\n\nMock verdict: the most complete learning path.\n\n"
        "Final approved answer: learn TCP/IP and Linux, practice on legal "
        "lab targets, document everything, and finish with a certification.",
    ],
    "Local (Ollama)": [
        "Mock answer from the local model to '{q}': build a home lab with "
        "virtual machines, break things safely, and keep a notebook of "
        "every technique that worked.",
        "Mock critique from the local model: the other answers need paid "
        "courses or cloud labs; a home lab costs nothing. My improved "
        "answer adds free, offline-friendly targets.",
        "WINNER: Claude\n\nMock verdict: the most complete learning path.\n\n"
        "Final approved answer: learn TCP/IP and Linux, practice on legal "
        "lab targets, document everything, and finish with a certification.",
    ],
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
        return [MockClient(name, replies=DEMO_SCRIPTS[name])
                for name in DEMO_SCRIPTS]
    clients = {"openai": OpenAIClient, "anthropic": AnthropicClient,
               "gemini": GeminiClient}
    participants = []
    for name, kind, model_var, key_var, default_model, base_url in PARTICIPANT_SPECS:
        key = env.get(key_var, "").strip()
        if not key:
            continue
        model = env.get(model_var, "").strip() or default_model
        if kind == "generic":
            participants.append(GenericChatClient(
                name, key, model=model, base_url=base_url, timeout=timeout))
        else:
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


CRITIQUE_PROMPT = (
    "You are in round 2 of a discussion. The original question and every "
    "participant's round 1 answer are below. Critique each answer briefly "
    "(factual errors, gaps, what it missed), then give your improved final "
    "answer.\n\nQuestion: {q}\n\nRound 1 answers:\n{answers}\n")

JUDGE_PROMPT = (
    "You are the judge. Pick the single best answer to the question below "
    "from the round 2 answers. Reply with the first line exactly "
    "'WINNER: <participant name>', then your reasons, then the final "
    "approved answer (you may lightly edit the winner for correctness)."
    "\n\nQuestion: {q}\n\nRound 2 answers:\n{answers}\n")

WINNER_RE = re.compile(r"^\s*WINNER:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)


def format_answers(answers):
    parts = []
    for answer in answers:
        body = answer.text if not answer.error else f"(error: {answer.error})"
        parts.append(f"--- {answer.name} ---\n{body}")
    return "\n\n".join(parts)


def parse_verdict(text, names):
    """Return (winner name or None, full verdict text).

    The winner is the name on the WINNER: line, matched case-insensitively
    against the participant names. Anything else means no clear winner.
    """
    match = WINNER_RE.search(text or "")
    if not match:
        return None, text
    claimed = match.group(1).strip()
    for name in names:
        if name.lower() == claimed.lower():
            return name, text
    return None, text


def pick_judge(participants, judge_name=None):
    """The judge is the named participant, else Claude, else the first."""
    if judge_name:
        for client in participants:
            if client.name.lower() == judge_name.lower():
                return client
    for client in participants:
        if client.name == "Claude":
            return client
    return participants[0] if participants else None


def run_discussion(participants, question, judge_name=None):
    """Round 1: everyone answers. Round 2: everyone critiques and improves.
    Round 3: the judge picks the best and writes the approved answer."""
    round1 = ask_all(participants, question)
    critique_prompt = CRITIQUE_PROMPT.format(
        q=question, answers=format_answers(round1))
    round2 = ask_all(participants, critique_prompt)
    judge = pick_judge(participants, judge_name)
    judge_prompt = JUDGE_PROMPT.format(q=question, answers=format_answers(round2))
    try:
        verdict = judge.ask(judge_prompt)
    except Exception as exc:  # noqa: BLE001 - a failed judge stops nothing
        verdict = Answer(judge.name, getattr(judge, "model", "?"),
                         error=f"unexpected: {redact(str(exc))}")
    winner, _ = parse_verdict(verdict.text, [p.name for p in participants])
    return {"rounds": [round1, round2], "judge": judge,
            "verdict": verdict, "winner": winner}


def render_discussion(question, result, asked_at=None):
    asked_at = asked_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["# Discussion: ask, critique, approve", "",
             f"Asked: {asked_at}", "", f"Question: {question}", ""]
    for number, answers in enumerate(result["rounds"], start=1):
        lines.append(f"## Round {number}")
        lines.append("")
        for answer in answers:
            lines.append(f"### {answer.name} (`{answer.model}`)")
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
    verdict = result["verdict"]
    lines.append(f"## Verdict (judge: {result['judge'].name})")
    lines.append("")
    if result["winner"]:
        lines.append(f"Winner: {result['winner']}")
        lines.append("")
    if verdict.error:
        lines.append(f"_Error: {verdict.error}_")
    else:
        lines.append(verdict.text)
    lines.append("")
    return "\n".join(lines)


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
    for name, _kind, _model_var, key_var, _default, _base_url in PARTICIPANT_SPECS:
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
    parser.add_argument("--mode", choices=["compare", "discuss"],
                        default="compare",
                        help="compare: ask all once. discuss: critique round "
                        "plus a judge who approves the best answer.")
    parser.add_argument("--judge", default=None,
                        help="judge participant for discuss mode "
                        "(default: Claude, else the first participant)")
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

    if args.mode == "compare":
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

    judge = pick_judge(participants, args.judge)
    calls = 2 * len(participants) + 1
    print(f"Discuss mode: round 1 ({len(participants)} answers), round 2 "
          f"({len(participants)} critiques), judge {judge.name}. "
          f"Up to {calls} provider calls.")
    result = run_discussion(participants, question, args.judge)
    for number, round_answers in enumerate(result["rounds"], start=1):
        print(f"\n########## ROUND {number} ##########")
        for answer in round_answers:
            print(f"\n===== {answer.name} (`{answer.model}`) =====")
            if answer.error:
                print(f"Error: {answer.error}")
            else:
                print(answer.text)
    verdict = result["verdict"]
    print(f"\n########## VERDICT (judge: {result['judge'].name}) ##########")
    if result["winner"]:
        print(f"Winner: {result['winner']}")
    if verdict.error:
        print(f"Error: {verdict.error}")
    else:
        print(verdict.text)

    if not args.no_save:
        path = save_transcript(args.save_dir, question,
                               render_discussion(question, result))
        print(f"\nTranscript saved: {path}")
    failed = all(a.error for a in result["rounds"][0]) or bool(verdict.error)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
