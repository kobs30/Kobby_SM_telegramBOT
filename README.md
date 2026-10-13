# Kobby Telegram Bridge

A Kobby Social Messenger bot that subscribes users to shared public mirrors of
Telegram channels. The webhook URL selects the source:

```text
https://<bridge-host>/t.me/alexavni
```

One stable Telegram channel ID maps to one Kobby channel on the configured Kobby
server. Each Telegram post is published once, regardless of subscriber count.
The service uses [saiTelegramChannels](https://github.com/kobs30/saiTelegramChannels)
for Telegram access. It does not maintain its own Telegram session.

Production example: [connect alexavni](https://tlg-bridge.164-68-127-58.sslip.io/t.me/alexavni).

## Connect in Kobby

1. Open **Settings → Add bot**.
2. Enter the source-specific webhook URL above.
3. Enable **Automatic connection** and the **Join communities** permission
   (`communities.join`). Leave other permissions and personal-chat access disabled;
   this service rejects broader grants.
4. Save and refresh the bot list. **Service connected** is acknowledged after
   the source has been checked, the mirror prepared, and your membership confirmed.
5. Read new posts in the public `tlg_<Telegram username>` channel.

Opening the URL in a browser displays instructions and the shared source status.
It does not subscribe anyone. A connection may initially show HTTP 503 while the
durable background job runs; Kobby retries automatically. Missing permissions or
an unavailable source never receive a successful connection acknowledgment.

## Authorization

- Each subscriber's **bot token** is obtained using `bot.connected` and the
  one-time-code exchange. It only joins that token's verified owner to the mirror.
- A dedicated **user account**, normally `tlg_bridge`, creates channels and sends
  posts through the ordinary user API. It does not use `/bot/messages`.
- The **watcher API key** connects Telegram sources and registers HMAC webhooks.

The publisher password is generated automatically, encrypted, and used to obtain
a new 30-day user session when necessary. There is no refresh-token assumption.
Passwords belonging to subscribing users are never requested.

## Delivery scope and limitations

- Public Telegram broadcast channels only; new sources start after their saved
  initial message boundary. Existing history is not imported.
- Text/captions and an original-post link are copied as plain text. Media and
  albums are not downloaded. A media-only post produces a readable link entry.
- Long text is split into parts of at most 48 KiB UTF-8, including attribution.
- Edits and deletions are not synchronized after a post has been accepted here.
- Prepared API requests and their IDs survive restarts and lost HTTP responses.
- A second subscriber or credential rotation reuses the same mirror.
- Leaving the Kobby channel does not delete it or trigger automatic rejoining.
- A deleted mirror or lost publisher role requires operator intervention; the
  service does not repeatedly create replacement channels.

**The upstream watcher retains only five posts per channel and deletes older
delivery jobs.** Polling reconciles only that retained window. An extended outage,
a burst of posts, or an upstream deletion can therefore cause unrecoverable gaps
before our service accepts an event. Our durable queue protects accepted posts,
not events already discarded upstream. Complete recovery requires a separate
watcher enhancement with retained delivery payloads and history pagination.
Arrival order upstream is not guaranteed; queued parts are sent in message-ID
order among the currently known unfinished posts.

## Local setup

Python 3.13 and one application process are required.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p .local
cp config.example.json .local/config.json
chmod 700 .local
chmod 600 .local/config.json
```

Edit the trusted HTTPS origins and file paths locally. Never place secrets in
URLs, repository files, command-line arguments, or chat messages.

Provision the account (registration must be enabled on Kobby):

```sh
.venv/bin/python -m bridge.cli --config .local/config.json provision --username tlg_bridge
```

The generated password is durably encrypted before registration. A lost response
is recovered by logging in with those same credentials. If the name belongs to
someone else, a random numeric suffix is used. Existing accounts are never adopted
by name alone. The command prints only the account name and expiry time.

Import the watcher key from a private file, then start the service:

```sh
.venv/bin/python -m bridge.cli --config .local/config.json set-watcher-key < /secure/path/watcher-key
BRIDGE_CONFIG=.local/config.json .venv/bin/uvicorn bridge.web:app --host 127.0.0.1 --port 9080 --workers 1 --no-access-log
```

Publish this process behind HTTPS on port 443. Both Kobby and the watcher reject
private webhook destinations. The bot supports one trusted Kobby server per
database; the database refuses to switch origins silently.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use simulated APIs and temporary databases; no real Telegram or Kobby user
chats are accessed. See [architecture](docs/ARCHITECTURE.md),
[deployment and operations](docs/OPERATIONS.md), and
[validation record](docs/VALIDATION.md).

The optional `scripts/smoke_live.py` acceptance check creates a dedicated Kobby
test account, connects the specified public source, and exercises publication in
a separate disposable channel. Run it only with authorization for those actions:

```sh
.venv/bin/python -m scripts.smoke_live --config .local/config.json \
  --test-data .local/acceptance --source alexavni
```

It deletes its bot, leaves the shared mirror, and deletes its validation channel.
Kobby has no account-deletion API; the test account and its protected credentials
remain. It never deletes a shared mirror or accesses personal conversations.
