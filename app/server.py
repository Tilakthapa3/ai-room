#!/usr/bin/env python3
"""Ai Room web app: real Google / Apple sign-in, per-user API keys.

Each person signs in with their own Google or Apple account, adds their
own AI provider keys on the settings screen, and then asks all the AIs,
compares the answers, or runs a discussion where a judge approves the
best one. Keys are stored per user in a local SQLite file and are never
sent back to the browser — the keys endpoint only reports set/unset.

Standard library only, except Apple Sign In, which needs the
`cryptography` package (ES256 client secret + RS256 token verification).
Google sign-in works with no extra dependencies.

Configuration lives in app/.env (see app/.env.example) and/or the real
environment:

    APP_BASE_URL      public URL of this app, e.g. http://localhost:8080
    SESSION_SECRET    long random string; sessions break if it changes
    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
    APPLE_CLIENT_ID   the Service ID, e.g. com.example.airoom.signin
    APPLE_TEAM_ID, APPLE_KEY_ID, APPLE_KEY_FILE (path to the .p8 key)

Usage:
    python3 app/server.py            # http://127.0.0.1:8080
    APP_PORT=9000 python3 app/server.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR)
sys.path.insert(0, ROOT_DIR)

import hub  # noqa: E402

hub.load_dotenv(os.path.join(ROOT_DIR, ".env"))
hub.load_dotenv(os.path.join(APP_DIR, ".env"))

BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8080").rstrip("/")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    print("WARNING: SESSION_SECRET is not set; using a random one. "
          "Sessions will not survive a restart. Set SESSION_SECRET in "
          "app/.env for real use.", file=sys.stderr)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
APPLE_CLIENT_ID = os.environ.get("APPLE_CLIENT_ID", "").strip()
APPLE_TEAM_ID = os.environ.get("APPLE_TEAM_ID", "").strip()
APPLE_KEY_ID = os.environ.get("APPLE_KEY_ID", "").strip()
APPLE_KEY_FILE = os.environ.get("APPLE_KEY_FILE", "").strip()
APPLE_APP_ID = os.environ.get("APPLE_APP_ID", "").strip()
DATA_DIR = os.environ.get("APP_DATA_DIR", os.path.join(APP_DIR, "data"))
SESSION_COOKIE = "ai_room_session"
STATE_COOKIE = "ai_room_oauth_state"
SESSION_TTL = 30 * 24 * 3600  # 30 days

KEY_FIELDS = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"]
OPTIONAL_FIELDS = ["OLLAMA_ENABLED", "OLLAMA_HOST", "OLLAMA_MODEL"]

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
APPLE_AUTH_URL = "https://appleid.apple.com/auth/authorize"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"


# ---------------------------------------------------------------- helpers

def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def post_form(url, fields, timeout=30):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"POST {url} failed ({exc.code}): {body[:300]}")


def get_json(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"GET {url} failed ({exc.code}): {body[:300]}")


# ---------------------------------------------------------------- sessions

def sign_session(payload: dict) -> str:
    body = b64url_encode(json.dumps(payload, separators=(",", ":"),
                                    sort_keys=True).encode("utf-8"))
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), body.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_session(cookie_value: str):
    """Return the session payload, or None if missing/tampered/expired."""
    if not cookie_value or "." not in cookie_value:
        return None
    body, _, sig = cookie_value.rpartition(".")
    expected = hmac.new(SESSION_SECRET.encode("utf-8"), body.encode("ascii"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig or ""):
        return None
    try:
        payload = json.loads(b64url_decode(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) <= time.time():
        return None
    return payload


def new_session_payload(provider, sub, email, name, picture):
    return {"provider": provider, "sub": sub, "email": email or "",
            "name": name or "", "picture": picture or "",
            "exp": int(time.time()) + SESSION_TTL}


def session_cookie_header(value, max_age=SESSION_TTL):
    secure = "; Secure" if BASE_URL.startswith("https://") else ""
    return (f"{SESSION_COOKIE}={value}; HttpOnly; SameSite=Lax; "
            f"Path=/; Max-Age={max_age}{secure}")


def clear_session_cookie_header():
    return (f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; "
            "Max-Age=0")


# ---------------------------------------------------------------- database

def db_path():
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, "app.db")


def init_db():
    conn = sqlite3.connect(db_path())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        " sub TEXT PRIMARY KEY, provider TEXT, email TEXT,"
        " name TEXT, picture TEXT, updated_at INTEGER)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_keys ("
        " sub TEXT, field TEXT, value TEXT,"
        " PRIMARY KEY (sub, field))")
    conn.commit()
    return conn


def upsert_user(provider, sub, email, name, picture):
    conn = init_db()
    conn.execute(
        "INSERT INTO users (sub, provider, email, name, picture, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(sub) DO UPDATE SET provider=excluded.provider,"
        " email=excluded.email, name=excluded.name,"
        " picture=excluded.picture, updated_at=excluded.updated_at",
        (sub, provider, email, name, picture, int(time.time())))
    conn.commit()
    conn.close()


def get_user_keys(sub):
    conn = init_db()
    rows = conn.execute(
        "SELECT field, value FROM user_keys WHERE sub = ?", (sub,)).fetchall()
    conn.close()
    return {field: value for field, value in rows}


def save_user_keys(sub, values):
    conn = init_db()
    for field, value in values.items():
        if value:
            conn.execute(
                "INSERT INTO user_keys (sub, field, value) VALUES (?, ?, ?)"
                " ON CONFLICT(sub, field) DO UPDATE SET value=excluded.value",
                (sub, field, value))
        else:
            conn.execute(
                "DELETE FROM user_keys WHERE sub = ? AND field = ?",
                (sub, field))
    conn.commit()
    conn.close()


def user_transcript_dir(sub):
    digest = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:12]
    path = os.path.join(DATA_DIR, "transcripts", digest)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- Google

def google_enabled():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def google_redirect_uri():
    return f"{BASE_URL}/auth/google/callback"


def build_google_auth_url(state):
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_google_code(code):
    """Exchange the code, verify the ID token, return (sub, email, name, picture)."""
    tokens = post_form(GOOGLE_TOKEN_URL, {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": google_redirect_uri(),
        "grant_type": "authorization_code",
    })
    id_token = tokens.get("id_token", "")
    if not id_token:
        raise RuntimeError("Google did not return an ID token")
    info = get_json(f"{GOOGLE_TOKENINFO_URL}?id_token={id_token}")
    if info.get("aud") != GOOGLE_CLIENT_ID:
        raise RuntimeError("Google ID token audience mismatch")
    if int(info.get("exp", 0)) <= time.time():
        raise RuntimeError("Google ID token is expired")
    email = info.get("email", "")
    if not email:
        raise RuntimeError("Google ID token has no email")
    return (info.get("sub", ""), email, info.get("name", ""),
            info.get("picture", ""))


# ---------------------------------------------------------------- Apple

def apple_enabled():
    return bool(APPLE_CLIENT_ID and APPLE_TEAM_ID
                and APPLE_KEY_ID and APPLE_KEY_FILE)


def apple_redirect_uri():
    return f"{BASE_URL}/auth/apple/callback"


def build_apple_auth_url(state):
    params = {
        "client_id": APPLE_CLIENT_ID,
        "redirect_uri": apple_redirect_uri(),
        "response_type": "code",
        "response_mode": "form_post",
        "scope": "name email",
        "state": state,
    }
    return f"{APPLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _require_cryptography():
    try:
        import cryptography  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Apple Sign In needs the cryptography package: "
            "pip install cryptography (see app/requirements.txt)") from exc


def apple_client_secret(now=None):
    """Build the ES256-signed client secret JWT Apple requires."""
    _require_cryptography()
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature)

    now = int(now if now is not None else time.time())
    header = {"alg": "ES256", "kid": APPLE_KEY_ID, "typ": "JWT"}
    claims = {"iss": APPLE_TEAM_ID, "iat": now, "exp": now + 3600,
              "aud": APPLE_ISSUER, "sub": APPLE_CLIENT_ID}
    signing_input = (
        f"{b64url_encode(json.dumps(header, separators=(',', ':')).encode())}"
        f".{b64url_encode(json.dumps(claims, separators=(',', ':')).encode())}"
    ).encode("ascii")
    with open(APPLE_KEY_FILE, "rb") as handle:
        key = serialization.load_pem_private_key(handle.read(), password=None)
    der_sig = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input.decode('ascii')}.{b64url_encode(raw_sig)}"


_jwks_cache = {"at": 0, "keys": []}


def apple_jwks():
    if time.time() - _jwks_cache["at"] < 3600 and _jwks_cache["keys"]:
        return _jwks_cache["keys"]
    data = get_json(APPLE_KEYS_URL)
    _jwks_cache.update({"at": time.time(), "keys": data.get("keys", [])})
    return _jwks_cache["keys"]


def verify_apple_id_token(id_token, client_id, jwks=None, now=None):
    """Verify an Apple RS256 ID token. Returns its claims.

    Signature is checked against Apple's published keys; issuer, audience
    and expiry are checked too. Raises RuntimeError on any failure.
    """
    _require_cryptography()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.exceptions import InvalidSignature

    now = int(now if now is not None else time.time())
    parts = id_token.split(".")
    if len(parts) != 3:
        raise RuntimeError("Apple ID token is malformed")
    try:
        header = json.loads(b64url_decode(parts[0]))
        claims = json.loads(b64url_decode(parts[1]))
        signature = b64url_decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Apple ID token is malformed") from exc
    jwks = apple_jwks() if jwks is None else jwks
    key = next((k for k in jwks if k.get("kid") == header.get("kid")
                and k.get("kty") == "RSA"), None)
    if key is None:
        raise RuntimeError("No matching Apple signing key")
    numbers = rsa.RSAPublicNumbers(
        int.from_bytes(b64url_decode(key["e"]), "big"),
        int.from_bytes(b64url_decode(key["n"]), "big"))
    public_key = numbers.public_key()
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(),
                          hashes.SHA256())
    except InvalidSignature as exc:
        raise RuntimeError("Apple ID token signature is invalid") from exc
    if claims.get("iss") != APPLE_ISSUER:
        raise RuntimeError("Apple ID token issuer mismatch")
    if claims.get("aud") != client_id:
        raise RuntimeError("Apple ID token audience mismatch")
    if int(claims.get("exp", 0)) <= now:
        raise RuntimeError("Apple ID token is expired")
    return claims


def exchange_apple_code(code):
    """Exchange the code, verify the ID token, return (sub, email, name)."""
    tokens = post_form(APPLE_TOKEN_URL, {
        "client_id": APPLE_CLIENT_ID,
        "client_secret": apple_client_secret(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": apple_redirect_uri(),
    })
    id_token = tokens.get("id_token", "")
    if not id_token:
        raise RuntimeError("Apple did not return an ID token")
    claims = verify_apple_id_token(id_token, APPLE_CLIENT_ID)
    email = claims.get("email", "")
    if not email:
        raise RuntimeError("Apple ID token has no email")
    return claims.get("sub", ""), email


# ---------------------------------------------------------------- HTTP layer

INDEX_PATH = os.path.join(APP_DIR, "static", "index.html")


def parse_cookies(header_value):
    cookies = {}
    for part in (header_value or "").split(";"):
        if "=" in part:
            name, value = part.strip().split("=", 1)
            cookies[name] = value
    return cookies


def answer_dict(answer):
    return {"name": answer.name, "model": answer.model, "text": answer.text,
            "error": answer.error, "seconds": answer.seconds,
            "usage": answer.usage}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout clean
        pass

    # -- small response helpers -------------------------------------
    def send_json(self, obj, status=200, extra_headers=()):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body, status=200, extra_headers=()):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location, extra_headers=()):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()

    def read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def read_form_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw.decode("utf-8")).items()}

    def session(self):
        cookies = parse_cookies(self.headers.get("Cookie"))
        return verify_session(cookies.get(SESSION_COOKIE, ""))

    def require_session(self):
        payload = self.session()
        if payload is None:
            self.send_json({"error": "not signed in"}, status=401)
            return None
        return payload

    # -- routing ------------------------------------------------------
    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - never hang the browser
            self.send_json({"error": f"server error: {exc}"}, status=500)

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"server error: {exc}"}, status=500)

    def _route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            with open(INDEX_PATH, "rb") as handle:
                self.send_html(handle.read())
            return
        if path == "/.well-known/apple-app-site-association":
            # Required by Sign in with Apple for the web: Apple fetches this
            # from your domain when the Service ID is configured.
            if not APPLE_APP_ID or not APPLE_TEAM_ID:
                self.send_json({"error": "not found"}, status=404)
                return
            self.send_json({"webcredentials":
                            {"apps": [f"{APPLE_TEAM_ID}.{APPLE_APP_ID}"]}})
            return
        if path == "/api/config":
            self.send_json({"google_enabled": google_enabled(),
                            "apple_enabled": apple_enabled(),
                            "base_url": BASE_URL})
            return
        if path == "/api/me":
            payload = self.require_session()
            if payload:
                self.send_json({k: payload.get(k, "") for k in
                                ("provider", "email", "name", "picture")})
            return
        if path == "/api/keys":
            payload = self.require_session()
            if payload:
                stored = get_user_keys(payload["sub"])
                self.send_json(
                    {field: bool(stored.get(field, "").strip())
                     for field in KEY_FIELDS + OPTIONAL_FIELDS})
            return
        if path == "/api/transcripts":
            payload = self.require_session()
            if payload:
                folder = user_transcript_dir(payload["sub"])
                names = sorted(f for f in os.listdir(folder)
                               if f.endswith(".md"))
                self.send_json({"transcripts": names})
            return
        if path.startswith("/api/transcripts/"):
            payload = self.require_session()
            if payload:
                name = os.path.basename(path[len("/api/transcripts/"):])
                folder = user_transcript_dir(payload["sub"])
                full = os.path.join(folder, name)
                if not name.endswith(".md") or not os.path.isfile(full):
                    self.send_json({"error": "not found"}, status=404)
                    return
                with open(full, encoding="utf-8") as handle:
                    self.send_json({"name": name, "text": handle.read()})
            return
        if path == "/auth/google":
            if not google_enabled():
                self.send_html("<h1>Google sign-in is not configured</h1>"
                               "<p>Set GOOGLE_CLIENT_ID and "
                               "GOOGLE_CLIENT_SECRET in app/.env.</p>",
                               status=503)
                return
            state = secrets.token_urlsafe(24)
            self.redirect(build_google_auth_url(state), extra_headers=[
                ("Set-Cookie", f"{STATE_COOKIE}={state}; HttpOnly; "
                 "SameSite=Lax; Path=/; Max-Age=600")])
            return
        if path == "/auth/google/callback":
            self._google_callback(query)
            return
        if path == "/auth/apple":
            if not apple_enabled():
                self.send_html("<h1>Apple sign-in is not configured</h1>"
                               "<p>Set APPLE_CLIENT_ID, APPLE_TEAM_ID, "
                               "APPLE_KEY_ID and APPLE_KEY_FILE in "
                               "app/.env.</p>", status=503)
                return
            state = secrets.token_urlsafe(24)
            self.redirect(build_apple_auth_url(state), extra_headers=[
                ("Set-Cookie", f"{STATE_COOKIE}={state}; HttpOnly; "
                 "SameSite=Lax; Path=/; Max-Age=600")])
            return
        if path == "/auth/logout":
            self.redirect("/", extra_headers=[
                ("Set-Cookie", clear_session_cookie_header())])
            return
        self.send_json({"error": "not found"}, status=404)

    def _google_callback(self, query):
        cookies = parse_cookies(self.headers.get("Cookie"))
        expected = cookies.get(STATE_COOKIE, "")
        state = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        error = (query.get("error") or [""])[0]
        if error or not code or not expected or state != expected:
            self.send_html("<h1>Sign-in failed</h1>"
                           "<p>Google returned an error or the request did "
                           "not match. <a href='/'>Try again</a>.</p>",
                           status=400)
            return
        try:
            sub, email, name, picture = exchange_google_code(code)
        except RuntimeError as exc:
            self.send_html(f"<h1>Sign-in failed</h1><p>{exc}</p>"
                           "<p><a href='/'>Try again</a>.</p>", status=400)
            return
        upsert_user("google", sub, email, name, picture)
        payload = new_session_payload("google", sub, email, name, picture)
        self.redirect("/", extra_headers=[
            ("Set-Cookie", session_cookie_header(sign_session(payload))),
            ("Set-Cookie", f"{STATE_COOKIE}=; Path=/; Max-Age=0")])

    def _route_post(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/auth/apple/callback":
            self._apple_callback()
            return
        if path == "/api/keys":
            payload = self.require_session()
            if payload:
                body = self.read_json_body()
                values = {field: str(body.get(field, "") or "").strip()
                          for field in KEY_FIELDS + OPTIONAL_FIELDS}
                save_user_keys(payload["sub"], values)
                self.send_json({"saved": True})
            return
        if path == "/api/ask":
            payload = self.require_session()
            if payload:
                self._handle_ask(payload)
            return
        self.send_json({"error": "not found"}, status=404)

    def _apple_callback(self):
        form = self.read_form_body()
        cookies = parse_cookies(self.headers.get("Cookie"))
        expected = cookies.get(STATE_COOKIE, "")
        if form.get("error") or not form.get("code") \
                or not expected or form.get("state") != expected:
            self.send_html("<h1>Sign-in failed</h1>"
                           "<p>Apple returned an error or the request did "
                           "not match. <a href='/'>Try again</a>.</p>",
                           status=400)
            return
        try:
            sub, email = exchange_apple_code(form["code"])
        except RuntimeError as exc:
            self.send_html(f"<h1>Sign-in failed</h1><p>{exc}</p>"
                           "<p><a href='/'>Try again</a>.</p>", status=400)
            return
        name = ""
        if form.get("user"):
            try:
                user_info = json.loads(form["user"])
                person = user_info.get("name", {})
                name = " ".join(part for part in
                                (person.get("firstName", ""),
                                 person.get("lastName", "")) if part)
            except ValueError:
                name = ""
        upsert_user("apple", sub, email, name, "")
        payload = new_session_payload("apple", sub, email, name, "")
        self.redirect("/", extra_headers=[
            ("Set-Cookie", session_cookie_header(sign_session(payload))),
            ("Set-Cookie", f"{STATE_COOKIE}=; Path=/; Max-Age=0")])

    def _handle_ask(self, payload):
        body = self.read_json_body()
        question = str(body.get("question", "") or "").strip()
        mode = body.get("mode") or "compare"
        judge = body.get("judge") or None
        if not question:
            self.send_json({"error": "a question is required"}, status=400)
            return
        if mode not in ("compare", "discuss"):
            self.send_json({"error": "unknown mode"}, status=400)
            return
        stored = get_user_keys(payload["sub"])
        env = dict(os.environ)
        for field in KEY_FIELDS + OPTIONAL_FIELDS:
            if field in stored:
                env[field] = stored[field]
        if "OLLAMA_ENABLED" not in stored:
            env.pop("OLLAMA_ENABLED", None)
        participants = hub.build_participants(env=env, timeout=120)
        if not participants:
            self.send_json({"error": "No AI providers configured. Add an "
                                     "API key in Settings first."},
                           status=400)
            return
        folder = user_transcript_dir(payload["sub"])
        if mode == "compare":
            answers = hub.ask_all(participants, question)
            text = hub.render_transcript(question, answers)
            path = hub.save_transcript(folder, question, text)
            self.send_json({"mode": mode,
                            "answers": [answer_dict(a) for a in answers],
                            "transcript": os.path.basename(path)})
            return
        result = hub.run_discussion(participants, question, judge)
        text = hub.render_discussion(question, result)
        path = hub.save_transcript(folder, question, text)
        self.send_json({"mode": mode,
                        "rounds": [[answer_dict(a) for a in round_answers]
                                   for round_answers in result["rounds"]],
                        "judge": result["judge"].name,
                        "winner": result["winner"],
                        "verdict": answer_dict(result["verdict"]),
                        "transcript": os.path.basename(path)})


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ai Room web app")
    parser.add_argument("--host", default=os.environ.get("APP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("APP_PORT", "8080")))
    args = parser.parse_args()
    init_db()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Ai Room running at {BASE_URL} (listening on "
          f"{args.host}:{args.port})")
    print(f"Google sign-in: {'on' if google_enabled() else 'off'}; "
          f"Apple sign-in: {'on' if apple_enabled() else 'off'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")


if __name__ == "__main__":
    main()
