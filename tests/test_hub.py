"""Tests for the ask-all hub. Standard library only, no keys, no network."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub
from providers import (AnthropicClient, Answer, GeminiClient, MockClient,
                       OllamaClient, OpenAIClient, ProviderError, redact,
                       _post_json)


class RedactTests(unittest.TestCase):
    def test_openai_key_masked(self):
        self.assertNotIn("SECRETKEY", redact("bad key sk-abcSECRETKEY here"))

    def test_anthropic_key_masked(self):
        self.assertNotIn("SECRETKEY", redact("sk-ant-api03-SECRETKEY"))

    def test_gemini_key_masked(self):
        self.assertNotIn("SECRETKEY", redact("AIzaSyD-SECRETKEY"))

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


if __name__ == "__main__":
    unittest.main()
