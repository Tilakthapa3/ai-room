# Ai Room web app

A web app for Ai Room: people sign in with their own Google or Apple
account, add their own AI provider keys, and then ask all the AIs,
compare the answers, or run a discussion where a judge approves the best
one.

Standard library only, except Apple Sign In, which needs the
`cryptography` package. No database server, no framework.

## Run it locally (Google sign-in works from localhost)

```bash
cp app/.env.example app/.env   # then fill in the values below
python3 app/server.py          # http://localhost:8080
```

Google allows `http://localhost:8080` redirect URIs for testing, so you
can try the full sign-in flow on your own computer before hosting
anything. Production needs a public `https://` URL.

## Install on your phone (free, no app store)

The app is installable as a web app (PWA): manifest, icons, and a
service worker are built in. It costs nothing and needs no store review.

**iPhone (Safari):** open the app's URL in Safari, tap Share, then
**Add to Home Screen**. It gets its own icon and opens full-screen like
an app. This works from any URL, including your computer's address on
your home Wi-Fi.

**Android (Chrome):** open the app's URL and tap **Install** when Chrome
offers it (or menu → Install app). Chrome only offers this over HTTPS
(or `localhost`), so for your phone use a free HTTPS tunnel — see below.

**Free HTTPS URL for your phone:** on the computer running the server,
install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect/downloads/)
and run `cloudflared tunnel --url http://localhost:8080`. It prints a
free `https://....trycloudflare.com` address. Set `APP_BASE_URL` to that
address, add `https://....trycloudflare.com/auth/google/callback` as a
redirect URI in Google Cloud (same step as localhost), restart, and open
the tunnel address on your phone. Note the tunnel address changes each
time you restart it.

## Setup 1: Sign in with Google (free, ~10 minutes)

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and
   sign in with your Gmail account. Create a project (any name, e.g.
   "Ai Room").
2. Left menu: **APIs & Services → OAuth consent screen**.
   - User type: **External**. App name: `Ai Room`. User support email:
     your Gmail. Leave the rest blank and save.
   - On the **Data Access / Scopes** step you do not need to add scopes:
     `openid`, `email`, and `profile` are included automatically.
   - Add yourself under **Audience / Test users** (your Gmail), save.
3. Left menu: **APIs & Services → Credentials → Create Credentials →
   OAuth client ID**.
   - Application type: **Web application**. Name: `Ai Room web`.
   - Under **Authorized redirect URIs** add exactly:
     - `http://localhost:8080/auth/google/callback` (for local testing)
     - `https://YOUR-DOMAIN/auth/google/callback` (once hosted; replace
       YOUR-DOMAIN with the real domain)
   - Create. Google shows a **Client ID** and **Client secret**.
4. Put them in `app/.env`:
   `GOOGLE_CLIENT_ID=...` and `GOOGLE_CLIENT_SECRET=...`.
5. Restart the server. The "Continue with Google" button is live. Note:
   while the app is in testing mode, only the test users you listed can
   sign in. To let anyone sign in, go back to the OAuth consent screen
   and publish the app.

## Setup 2: Sign in with Apple (paid Apple Developer account, $99/year)

Apple requires an Apple Developer Program membership. Without it you
cannot create the pieces below — the Apple button stays off and Google
sign-in still works.

1. Sign in at [developer.apple.com/account](https://developer.apple.com/account)
   with your Apple ID (membership required).
2. **Certificates, Identifiers & Profiles → Identifiers → +**:
   - Register an **App ID**: bundle ID e.g. `com.example.airoom`
     (use your own reverse-domain). Tick the **Sign in with Apple**
     capability. Save. Note the bundle ID — it is `APPLE_APP_ID`.
3. **Identifiers → +** again: register a **Services ID**, e.g.
   `com.example.airoom.signin`. Tick **Sign in with Apple → Configure**:
   - Primary App ID: the App ID from step 2.
   - Website URLs: your domain, e.g. `https://YOUR-DOMAIN` (for local
     testing Apple requires a real domain — localhost does not work for
     Apple). Return URLs: `https://YOUR-DOMAIN/auth/apple/callback`.
   - Save. Note the Services ID — it is `APPLE_CLIENT_ID`.
4. **Certificates, Identifiers & Profiles → Keys → +**: name it
   `Ai Room Sign In`, tick **Sign in with Apple → Configure** (choose
   the Primary App ID from step 2), save, then **download the .p8 key
   file**. Apple shows it once. Note the **Key ID** (`APPLE_KEY_ID`)
   and your **Team ID** (`APPLE_TEAM_ID`, top right of the account page).
5. Put the .p8 file somewhere private on the server (never in the repo),
   and set in `app/.env`:
   `APPLE_CLIENT_ID`, `APPLE_TEAM_ID`, `APPLE_KEY_ID`,
   `APPLE_KEY_FILE=/path/to/AuthKey_XXXX.p8`, `APPLE_APP_ID`.
6. Install the one dependency and restart:
   `pip install -r app/requirements.txt`
7. The app serves `/.well-known/apple-app-site-association` automatically
   from `APPLE_TEAM_ID` + `APPLE_APP_ID` — Apple fetches it from your
   domain to confirm the setup.

Apple sends the user's name only on the very first sign-in, and the
email may be a private relay address if the user chose "Hide My Email".
Both are normal.

## How accounts and keys work

- Signing in creates a session cookie (HMAC-signed, HttpOnly, 30 days).
  The server never sees a Google/Apple password — the provider only
  returns a signed ID token, which the server verifies (Google via its
  tokeninfo endpoint; Apple via signature check against Apple's keys).
- Each signed-in user adds their own keys on the Settings screen. Keys
  are stored per user in `app/data/app.db` (SQLite) and are never sent
  back to the browser; the settings screen only shows set/unset.
- Transcripts are saved per user under `app/data/transcripts/`.
- Every real question is billed to that user's own keys, on the
  providers they configured.

## Security notes (read before putting this on the internet)

- Serve it over HTTPS with a reverse proxy (e.g. Caddy or nginx) and
  set `APP_BASE_URL` to the `https://` address. Session cookies are
  marked Secure only when the base URL is https.
- Set a long random `SESSION_SECRET`. Anyone who knows it can forge
  sessions.
- `app/data/` holds the SQLite file with users' API keys. Protect that
  directory, back it up carefully, and never publish it. Keys are stored
  unencrypted at rest in this version; encrypting them at rest is a
  planned follow-up.
- `app/.env` and any `.p8` key file must never be committed to git.
- The app only ever *asks* the AI providers. It cannot browse, run
  commands, or act for the user.

## Tests

```bash
python3 -m unittest discover -s app/tests -v
```
