# Ask-All Hub

Ask every configured AI the same question, side by side, and compare the
answers. One question goes to ChatGPT, Claude, Gemini, Grok, DeepSeek,
Mistral, OpenRouter, and optionally a local Ollama model. Answers print
as they arrive and are saved as a Markdown transcript. A participant
that fails never stops the rest.

Standard library only — no dependencies, no framework. Keys live only in
a `.env` file on your machine, never in the repo and never in chat.

## What it does not do

There is no public API key for Muse the assistant, so Muse cannot join
as a participant. The participants are the providers with real APIs:
OpenAI, Anthropic, Google, xAI, DeepSeek, Mistral, OpenRouter, and
local models through Ollama.

## Setup

```bash
cp .env.example .env
```

Then put your keys in `.env`:

| Key | Where to get it |
|---|---|
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `XAI_API_KEY` | https://console.x.ai |
| `DEEPSEEK_API_KEY` | https://platform.deepseek.com/api_keys |
| `MISTRAL_API_KEY` | https://console.mistral.ai/api-keys |
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys |

For the local participant, install [Ollama](https://ollama.com), pull a
model once (`ollama pull qwen2.5:7b`), and set `OLLAMA_ENABLED=true` in
`.env`. It needs no key and nothing leaves your machine.

## Run

```bash
# Ask the configured participants
python3 hub.py --ask "What is the best way to learn penetration testing?"

# See which participants are configured (keys are never printed)
python3 hub.py --list

# Try it with no keys and no network
python3 hub.py --demo --ask "Why is the sky blue?"
```

Answers save to `transcripts/` as Markdown. Use `--no-save` to skip that,
and `--timeout 120` if a slow model needs longer.

## Modes

### Compare (default)

Ask every participant once, print the answers side by side, save a
transcript. This is the cheapest mode: one call per participant.

```bash
python3 hub.py --ask "What is the best way to learn penetration testing?"
```

### Discuss: talk, compare, approve the best

Three rounds, so the AIs talk to each other instead of answering in
isolation:

1. **Round 1** — every participant answers the question.
2. **Round 2** — every participant sees all the round 1 answers, critiques
   them (errors, gaps, what they missed), and gives an improved answer.
3. **Verdict** — a judge reads the round 2 answers and approves the best
   one: first line `WINNER: <name>`, then reasons, then the final approved
   answer. The judge may lightly edit the winner for correctness.

```bash
python3 hub.py --mode discuss --ask "How do I start learning penetration testing?"
python3 hub.py --mode discuss --judge ChatGPT --ask "..."   # pick the judge
```

The default judge is Claude when it is configured, otherwise the first
participant. If the judge's reply has no clear `WINNER:` line, the
transcript still records the verdict and no winner is claimed.

**Cost:** discuss mode makes up to `2N + 1` provider calls (N answers, N
critiques, 1 verdict), so it costs roughly twice the compare mode plus
one call. The hub prints the call count before it starts. A failed
participant never stops the rest — its rounds are recorded as errors.

## Cost

Every real question costs money on every provider you configured — a few
cents per question at the default models, more on the bigger ones. Start
with the defaults (the cheapest sensible model per provider) and check
each provider's usage page after your first runs. The `--demo` mode is
free and is the right way to test the loop first.

## Models

Defaults (verified against provider docs, November 2026):

- ChatGPT: `gpt-4o-mini`
- Claude: `claude-sonnet-5-5`
- Gemini: `gemini-3.8-flash`
- Grok: `grok-4-1-fast`
- DeepSeek: `deepseek-flash`
- Mistral: `mistral-small-latest`
- OpenRouter: `openrouter/auto` (a router fallback — pick a full model slug)
- Ollama: `qwen2.5:7b`

Model IDs change over time. Override any of them in `.env` with
`OPENAI_MODEL`, `ANTHROPIC_MODEL`, `GEMINI_MODEL`, `XAI_MODEL`,
`DEEPSEEK_MODEL`, `MISTRAL_MODEL`, `OPENROUTER_MODEL`, or `OLLAMA_MODEL`.
If a provider answers 404 for a model, that is almost always a renamed
or retired model ID — check the provider's model list and update `.env`.

## Safety

- `.env` is gitignored. If a key ever lands in a file by mistake, revoke
  it at the provider and make a new one; deleting the file is not enough.
- Error messages have key-like strings masked before they are printed or
  saved, but treat any log output as sensitive anyway.
- The hub only ever *asks*. It cannot browse, run commands, or act for
  you — each provider answers the one question and stops.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Roadmap

- Discuss mode: participants critique each other's answers, then a judge
  approves the best one. Done — see Modes above.
- Relay mode: each answer builds on the previous one.
- A synthesis step for ask-all mode: one participant summarizes where
  the answers agree and where they differ.
