"""Tests for the ask-all hub. Standard library only, no keys, no network."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub
from providers import (AnthropicClient, Answer, GeminiClient,
                       GenericChatClient, MockClient, OllamaClient,
                       OpenAIClient, ProviderError, redact, _post_json)


class RedactTests(unittest.TestCase):
    def test_openai_key_masked(self):
        self.assertNotIn("SECRETKEY", redact("bad key sk-abcSECRETKEY here"))

    def test_anthropic_key_masked(self):
        self.assertNotIn("SECRETKEY", redact("sk-ant-api03-SECRETKEY"))

    def test_gemini_key_masked(self):
        self.assertNotIn("SECRETKEY", redact("AIzaSyD-SECRETKEY"))

    def test_xai_key_masked(self):
        self.assertNotIn("SECRETKEY", redact("bad key xai-abcSECRETKEY here"))

    def test_plain_text_untouched(self):
        self.assertEqual(redact("plain message"), "plain message")


class DotenvTests(unittest.TestCase):
    def test_loads_values_without_overriding_real_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# comment\nOPENAI_API_KEY=test-key\n"
                             "QUOTED=\"hello\"\nEMPTY=\n")
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "real"},
                                 clear=False):
                hub.load_dotenv(path)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "real")
            with mock.patch.dict(os.environ, {}, clear=True):
                hub.load_dotenv(path)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "test-key")
                self.assertEqual(os.environ["QUOTED"], "hello")

    def test_missing_file_is_fine(self):
        hub.load_dotenv("/nonexistent/.env")  # must not raise


class ParticipantTests(unittest.TestCase):
    def test_only_keyed_participants_included(self):
        env = {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": ""}
        parts = hub.build_participants(env=env)
        self.assertEqual([p.name for p in parts], ["ChatGPT"])

    def test_model_override_respected(self):
        env = {"OPENAI_API_KEY": "k", "OPENAI_MODEL": "gpt-4o"}
        parts = hub.build_participants(env=env)
        self.assertEqual(parts[0].model, "gpt-4o")

    def test_ollama_opt_in_only(self):
        env = {"OLLAMA_ENABLED": "true", "OLLAMA_MODEL": "llama3:8b"}
        parts = hub.build_participants(env=env)
        self.assertEqual(len(parts), 1)
        self.assertIsInstance(parts[0], OllamaClient)
        self.assertEqual(parts[0].model, "llama3:8b")
        self.assertEqual(hub.build_participants(env={}), [])

    def test_demo_builds_all_mocks(self):
        parts = hub.build_participants(env={}, demo=True)
        self.assertEqual(len(parts), 4)
        self.assertTrue(all(isinstance(p, MockClient) for p in parts))


class ProviderShapeTests(unittest.TestCase):
    def _fake_post(self, response):
        calls = {}

        def fake(url, headers, payload, timeout, provider):
            calls["url"] = url
            calls["headers"] = headers
            calls["payload"] = payload
            return response

        return fake, calls

    def test_openai_request_and_parse(self):
        fake, calls = self._fake_post(
            {"choices": [{"message": {"content": "hi"}}],
             "usage": {"total_tokens": 9}})
        with mock.patch("providers._post_json", side_effect=fake):
            ans = OpenAIClient("k", model="gpt-4o-mini").ask("hello?")
        self.assertEqual(ans.text, "hi")
        self.assertEqual(ans.usage, {"total_tokens": 9})
        self.assertIn("chat/completions", calls["url"])
        self.assertEqual(calls["headers"]["Authorization"], "Bearer k")
        self.assertEqual(calls["payload"]["messages"][0]["content"], "hello?")

    def test_anthropic_request_and_parse(self):
        fake, calls = self._fake_post(
            {"content": [{"type": "text", "text": "yo"}],
             "usage": {"input_tokens": 3}})
        with mock.patch("providers._post_json", side_effect=fake):
            ans = AnthropicClient("k").ask("hello?")
        self.assertEqual(ans.text, "yo")
        self.assertEqual(calls["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(calls["payload"]["max_tokens"], 1024)

    def test_gemini_request_and_parse(self):
        fake, calls = self._fake_post(
            {"candidates": [{"content": {"parts": [{"text": "namaste"}]}}],
             "usageMetadata": {"totalTokenCount": 5}})
        with mock.patch("providers._post_json", side_effect=fake):
            ans = GeminiClient("k", model="gemini-3.8-flash").ask("hello?")
        self.assertEqual(ans.text, "namaste")
        self.assertIn("gemini-3.8-flash:generateContent", calls["url"])
        self.assertEqual(calls["headers"]["x-goog-api-key"], "k")

    def test_ollama_request_and_parse(self):
        fake, calls = self._fake_post({"message": {"content": "local hi"}})
        with mock.patch("providers._post_json", side_effect=fake):
            ans = OllamaClient(model="qwen2.5:7b").ask("hello?")
        self.assertEqual(ans.text, "local hi")
        self.assertFalse(calls["payload"]["stream"])

    def test_http_error_becomes_answer_error(self):
        def boom(url, headers, payload, timeout, provider):
            raise ProviderError(provider, 401, "bad key")

        with mock.patch("providers._post_json", side_effect=boom):
            ans = OpenAIClient("bad").ask("hello?")
        self.assertIn("401", ans.error)
        self.assertEqual(ans.text, "")

    def test_key_never_leaks_into_error(self):
        def boom(url, headers, payload, timeout, provider):
            raise ProviderError(provider, 400, "key sk-ant-api03-SECRETKEY bad")

        with mock.patch("providers._post_json", side_effect=boom):
            ans = AnthropicClient("sk-ant-api03-SECRETKEY").ask("hello?")
        self.assertNotIn("SECRETKEY", ans.error)


class GenericProviderTests(unittest.TestCase):
    def test_all_four_generic_participants_built(self):
        env = {"XAI_API_KEY": "x", "DEEPSEEK_API_KEY": "d",
               "MISTRAL_API_KEY": "m", "OPENROUTER_API_KEY": "o"}
        parts = hub.build_participants(env=env)
        self.assertEqual([p.name for p in parts],
                         ["Grok", "DeepSeek", "Mistral", "OpenRouter"])
        self.assertTrue(all(isinstance(p, GenericChatClient) for p in parts))
        self.assertEqual([p.model for p in parts],
                         ["grok-4-1-fast", "deepseek-flash",
                          "mistral-small-latest", "openrouter/auto"])
        self.assertEqual([p.base_url for p in parts],
                         ["https://api.x.ai/v1",
                          "https://api.deepseek.com/v1",
                          "https://api.mistral.ai/v1",
                          "https://openrouter.ai/api/v1"])

    def test_generic_model_override_respected(self):
        env = {"XAI_API_KEY": "x", "XAI_MODEL": "grok-4"}
        parts = hub.build_participants(env=env)
        self.assertEqual(parts[0].model, "grok-4")

    def test_generic_request_uses_own_base_url_and_name(self):
        calls = {}

        def fake(url, headers, payload, timeout, provider):
            calls["url"] = url
            calls["headers"] = headers
            calls["provider"] = provider
            return {"choices": [{"message": {"content": "grok hi"}}],
                    "usage": {}}

        client = GenericChatClient("Grok", "xai-k", model="grok-4-1-fast",
                                   base_url="https://api.x.ai/v1")
        with mock.patch("providers._post_json", side_effect=fake):
            ans = client.ask("hello?")
        self.assertEqual(ans.name, "Grok")
        self.assertEqual(ans.text, "grok hi")
        self.assertEqual(calls["url"],
                         "https://api.x.ai/v1/chat/completions")
        self.assertEqual(calls["headers"]["Authorization"], "Bearer xai-k")
        self.assertEqual(calls["provider"], "Grok")

    def test_generic_key_never_leaks_into_error(self):
        def boom(url, headers, payload, timeout, provider):
            raise ProviderError(provider, 401, "key xai-abcSECRETKEY bad")

        client = GenericChatClient("Grok", "xai-abcSECRETKEY",
                                   model="grok-4-1-fast",
                                   base_url="https://api.x.ai/v1")
        with mock.patch("providers._post_json", side_effect=boom):
            ans = client.ask("hello?")
        self.assertNotIn("SECRETKEY", ans.error)


class AskAllTests(unittest.TestCase):
    def test_failure_of_one_never_stops_the_rest(self):
        bad = MockClient("Bad")
        bad.ask = lambda q: (_ for _ in ()).throw(RuntimeError("boom"))
        good = MockClient("Good", reply="fine")
        answers = hub.ask_all([bad, good], "q?")
        self.assertEqual(len(answers), 2)
        self.assertTrue(answers[0].error)
        self.assertEqual(answers[1].text, "fine")

    def test_transcript_contains_question_answers_and_errors(self):
        answers = [Answer("ChatGPT", "gpt-4o-mini", text="hello",
                          usage={"total_tokens": 3}, seconds=1.2),
                   Answer("Claude", "claude-sonnet-5-5", error="HTTP 401: bad")]
        text = hub.render_transcript("What?", answers, asked_at="2026-11-03")
        self.assertIn("What?", text)
        self.assertIn("ChatGPT", text)
        self.assertIn("hello", text)
        self.assertIn("HTTP 401", text)

    def test_key_never_leaks_into_transcript(self):
        answers = [Answer("Claude", "m", error="key sk-ant-api03-SECRETKEY")]
        text = hub.render_transcript("q", answers)
        self.assertNotIn("SECRETKEY", redact(text))

    def test_save_transcript_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = hub.save_transcript(tmp, "Hello world?", "# test")
            self.assertTrue(os.path.exists(path))
            self.assertIn("hello-world", path)


class VerdictTests(unittest.TestCase):
    def test_winner_parsed_and_matched_case_insensitively(self):
        winner, _ = hub.parse_verdict(
            "WINNER: claude\n\nBecause reasons.", ["ChatGPT", "Claude"])
        self.assertEqual(winner, "Claude")

    def test_no_winner_line_means_no_winner(self):
        winner, text = hub.parse_verdict("Just some text.",
                                         ["ChatGPT", "Claude"])
        self.assertIsNone(winner)
        self.assertEqual(text, "Just some text.")

    def test_unknown_name_means_no_winner(self):
        winner, _ = hub.parse_verdict("WINNER: Someone Else",
                                      ["ChatGPT", "Claude"])
        self.assertIsNone(winner)

    def test_pick_judge_prefers_named_then_claude_then_first(self):
        chat, claude = MockClient("ChatGPT"), MockClient("Claude")
        self.assertIs(hub.pick_judge([chat, claude], "chatgpt"), chat)
        self.assertIs(hub.pick_judge([chat, claude]), claude)
        self.assertIs(hub.pick_judge([chat]), chat)


class DiscussionTests(unittest.TestCase):
    def _scripted(self, name, replies):
        return MockClient(name, replies=replies)

    def test_full_discussion_flow(self):
        chat = self._scripted("ChatGPT", ["a1", "a2", "WINNER: Claude\nok"])
        claude = self._scripted(
            "Claude", ["b1", "b2", "WINNER: Claude\n\nBest.\n\nFinal."])
        result = hub.run_discussion([chat, claude], "What?")
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(result["winner"], "Claude")
        self.assertIn("Final.", result["verdict"].text)
        # The judge saw the round 2 answers in its prompt.
        self.assertIn("a2", claude.prompts[-1])
        self.assertIn("b2", claude.prompts[-1])
        # The critique round saw the round 1 answers.
        self.assertIn("a1", chat.prompts[1])
        self.assertIn("b1", chat.prompts[1])

    def test_discussion_survives_a_broken_participant(self):
        class ExplodingClient(MockClient):
            def ask(self, question):
                raise RuntimeError("boom")

        good = MockClient("Good", replies=["g1", "g2", "WINNER: Good\nfine"])
        result = hub.run_discussion([ExplodingClient("Bad"), good], "What?",
                                    judge_name="Good")
        self.assertTrue(result["rounds"][0][0].error)
        self.assertTrue(result["rounds"][1][0].error)
        self.assertEqual(result["winner"], "Good")

    def test_discussion_transcript_has_rounds_and_verdict(self):
        chat = self._scripted("ChatGPT", ["a1", "a2", "WINNER: Claude\nok"])
        claude = self._scripted("Claude", ["b1", "b2", "WINNER: Claude\nwin"])
        result = hub.run_discussion([chat, claude], "What?")
        text = hub.render_discussion("What?", result, asked_at="2026-11-03")
        self.assertIn("## Round 1", text)
        self.assertIn("## Round 2", text)
        self.assertIn("## Verdict", text)
        self.assertIn("Winner: Claude", text)


if __name__ == "__main__":
    unittest.main()
