"""Tests for the Ai Room web app. Standard library only, no network.

Apple tests adapt: with the `cryptography` package installed they verify
real JWT building/checking; without it they verify the clear error.
"""

import base64
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

os.environ["SESSION_SECRET"] = "test-secret-do-not-use-in-production"
os.environ["APP_DATA_DIR"] = os.path.join(tempfile.gettempdir(),
                                         "ai-room-app-test-data")
os.environ["APP_BASE_URL"] = "http://localhost:8080"

import server  # noqa: E402
import hub  # noqa: E402


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class CookieTests(unittest.TestCase):
    def test_sign_and_verify_round_trip(self):
        payload = server.new_session_payload("google", "sub1", "a@b.c",
                                             "Ann", "")
        value = server.sign_session(payload)
        back = server.verify_session(value)
        self.assertEqual(back["email"], "a@b.c")
        self.assertEqual(back["sub"], "sub1")

    def test_tampered_cookie_rejected(self):
        payload = server.new_session_payload("google", "sub1", "a@b.c",
                                             "Ann", "")
        value = server.sign_session(payload)
        body, _, _sig = value.rpartition(".")
        tampered = json.loads(base64.urlsafe_b64decode(
            body + "=" * (-len(body) % 4)))
        tampered["email"] = "mallory@evil.example"
        forged = b64url(json.dumps(tampered).encode()) + ".deadbeef"
        self.assertIsNone(server.verify_session(forged))

    def test_expired_cookie_rejected(self):
        payload = {"provider": "google", "sub": "s", "email": "a@b.c",
                   "name": "", "picture": "", "exp": int(time.time()) - 10}
        self.assertIsNone(server.verify_session(server.sign_session(payload)))


class GoogleUrlTests(unittest.TestCase):
    def test_auth_url_has_client_redirect_scope_and_state(self):
        with mock.patch.object(server, "GOOGLE_CLIENT_ID", "cid123"):
            url = server.build_google_auth_url("state-xyz")
        self.assertIn("accounts.google.com", url)
        self.assertIn("client_id=cid123", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Fauth%2Fgoogle%2Fcallback", url)
        self.assertIn("scope=openid+email+profile", url)
        self.assertIn("state=state-xyz", url)

    def test_apple_auth_url_uses_form_post(self):
        with mock.patch.object(server, "APPLE_CLIENT_ID", "com.example.svc"):
            url = server.build_apple_auth_url("state-abc")
        self.assertIn("appleid.apple.com", url)
        self.assertIn("response_mode=form_post", url)
        self.assertIn("scope=name+email", url)


class AppleCryptoTests(unittest.TestCase):
    def test_client_secret_or_clear_error(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError:
            with self.assertRaises(RuntimeError) as ctx:
                server.apple_client_secret()
            self.assertIn("cryptography", str(ctx.exception))
            return
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        with tempfile.NamedTemporaryFile("wb", delete=False,
                                         suffix=".p8") as handle:
            handle.write(pem)
            key_path = handle.name
        try:
            with mock.patch.object(server, "APPLE_KEY_FILE", key_path), \
                    mock.patch.object(server, "APPLE_TEAM_ID", "TEAM123456"), \
                    mock.patch.object(server, "APPLE_KEY_ID", "KEY1234567"), \
                    mock.patch.object(server, "APPLE_CLIENT_ID", "com.example.svc"):
                secret = server.apple_client_secret(now=1_700_000_000)
            parts = secret.split(".")
            self.assertEqual(len(parts), 3)
            header = json.loads(base64.urlsafe_b64decode(
                parts[0] + "=" * (-len(parts[0]) % 4)))
            self.assertEqual(header["kid"], "KEY1234567")
            self.assertEqual(header["alg"], "ES256")
            claims = json.loads(base64.urlsafe_b64decode(
                parts[1] + "=" * (-len(parts[1]) % 4)))
            self.assertEqual(claims["iss"], "TEAM123456")
            self.assertEqual(claims["aud"], "https://appleid.apple.com")
        finally:
            os.unlink(key_path)

    def test_verify_apple_token_or_clear_error(self):
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError:
            with self.assertRaises(RuntimeError):
                server.verify_apple_id_token("a.b.c", "com.example.svc")
            return
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub = key.public_key().public_numbers()
        n = b64url(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big"))
        e = b64url(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big"))
        jwks = [{"kty": "RSA", "kid": "kid1", "use": "sig", "alg": "RS256",
                 "n": n, "e": e}]
        header = b64url(json.dumps({"alg": "RS256", "kid": "kid1",
                                    "typ": "JWT"}).encode())
        claims = {"iss": "https://appleid.apple.com",
                  "aud": "com.example.svc",
                  "exp": int(time.time()) + 600,
                  "sub": "user123", "email": "person@example.com"}
        payload = b64url(json.dumps(claims).encode())
        signing_input = f"{header}.{payload}".encode("ascii")
        sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        token = f"{header}.{payload}.{b64url(sig)}"
        out = server.verify_apple_id_token(token, "com.example.svc",
                                           jwks=jwks)
        self.assertEqual(out["email"], "person@example.com")
        with self.assertRaises(RuntimeError):
            server.verify_apple_id_token(token, "com.other.svc", jwks=jwks)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(server, "DATA_DIR", self.tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_user_and_keys_round_trip(self):
        server.upsert_user("google", "sub9", "x@y.z", "X", "")
        server.save_user_keys("sub9", {"OPENAI_API_KEY": "sk-test",
                                       "GEMINI_API_KEY": ""})
        stored = server.get_user_keys("sub9")
        self.assertEqual(stored.get("OPENAI_API_KEY"), "sk-test")
        self.assertNotIn("GEMINI_API_KEY", stored)
        server.save_user_keys("sub9", {"OPENAI_API_KEY": ""})
        self.assertNotIn("OPENAI_API_KEY", server.get_user_keys("sub9"))

    def test_new_provider_keys_round_trip(self):
        server.upsert_user("google", "sub10", "y@z.w", "Y", "")
        server.save_user_keys("sub10", {"XAI_API_KEY": "xai-test",
                                        "DEEPSEEK_API_KEY": "ds-test",
                                        "MISTRAL_API_KEY": "m-test",
                                        "OPENROUTER_API_KEY": "or-test"})
        stored = server.get_user_keys("sub10")
        self.assertEqual(stored.get("XAI_API_KEY"), "xai-test")
        self.assertEqual(stored.get("DEEPSEEK_API_KEY"), "ds-test")
        self.assertEqual(stored.get("MISTRAL_API_KEY"), "m-test")
        self.assertEqual(stored.get("OPENROUTER_API_KEY"), "or-test")
        self.assertIn("XAI_API_KEY", server.KEY_FIELDS)
        server.save_user_keys("sub10", {"XAI_API_KEY": ""})
        self.assertNotIn("XAI_API_KEY", server.get_user_keys("sub10"))


class PerUserEnvTests(unittest.TestCase):
    def test_participants_from_per_user_env_only(self):
        env = {"OPENAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "sk-y"}
        names = [p.name for p in hub.build_participants(env=env)]
        self.assertEqual(names, ["ChatGPT", "Claude"])

    def test_no_keys_no_participants(self):
        self.assertEqual(hub.build_participants(env={}), [])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._patch = mock.patch.object(server, "DATA_DIR", cls.tmp.name)
        cls._patch.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        payload = server.new_session_payload("google", "sub1",
                                             "tester@example.com", "Tester", "")
        cls.cookie = server.sign_session(payload)
        server.upsert_user("google", "sub1", "tester@example.com",
                           "Tester", "")
        server.save_user_keys("sub1", {"OPENAI_API_KEY": "sk-secret-123"})

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._patch.stop()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def test_index_serves(self):
        status, _headers, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Ai Room", data)
        self.assertIn(b'rel="manifest"', data)

    def test_manifest_served(self):
        status, _headers, data = self.request("GET", "/manifest.json")
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertEqual(body["name"], "Ai Room")
        self.assertEqual(body["display"], "standalone")

    def test_service_worker_served(self):
        status, _headers, data = self.request("GET", "/sw.js")
        self.assertEqual(status, 200)
        self.assertIn(b"ai-room-v1", data)

    def test_icon_is_a_real_png(self):
        status, headers, data = self.request("GET", "/icon-192.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(len(server.make_icon_png(192)), len(data))

    def test_me_requires_session(self):
        status, _headers, _data = self.request("GET", "/api/me")
        self.assertEqual(status, 401)
        status, _headers, data = self.request(
            "GET", "/api/me",
            headers={"Cookie": f"{server.SESSION_COOKIE}={self.cookie}"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["email"], "tester@example.com")

    def test_keys_endpoint_never_returns_values(self):
        _s, _h, data = self.request(
            "GET", "/api/keys",
            headers={"Cookie": f"{server.SESSION_COOKIE}={self.cookie}"})
        self.assertNotIn(b"sk-secret-123", data)
        body = json.loads(data)
        self.assertTrue(body["OPENAI_API_KEY"])
        self.assertFalse(body["ANTHROPIC_API_KEY"])

    def test_ask_requires_session(self):
        status, _headers, _data = self.request(
            "POST", "/api/ask", body=json.dumps({"question": "hi?"}),
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 401)

    def test_google_auth_redirect_when_configured(self):
        with mock.patch.object(server, "GOOGLE_CLIENT_ID", "cid"), \
                mock.patch.object(server, "GOOGLE_CLIENT_SECRET", "sec"):
            status, headers, _data = self.request("GET", "/auth/google")
        self.assertEqual(status, 302)
        self.assertIn("accounts.google.com", headers["Location"])
        self.assertIn("ai_room_oauth_state=", headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
