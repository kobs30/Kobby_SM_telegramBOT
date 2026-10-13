# Validation record

## Automated checks

Offline tests exercise first and second subscriptions, concurrent first requests,
credential rotation, stale events, lost channel-create responses, lost publication
responses, restart with pending work, edited retry payloads, unavailable sources,
publisher session renewal/failure, missing/revoked join permission, additional
permissions and chat scopes, authenticated discard of chat webhooks, deleted mirrors,
Unicode splitting, media-only posts, HMAC checks, out-of-order intake, encrypted
secrets, origin binding, and HTTP routing.

Run:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The test APIs are simulated. Tests do not access personal conversations. In a
restricted sandbox the threaded ASGI TestClient may require permission for local
event-loop sockets; no external API access is used by these tests.

## Live deployment checks

Validated on 2026-10-13:

- The initial 18 offline tests passed, including registration response loss,
  foreign-name collision, disabled registration, and the original restrictive
  permission policy (superseded by the compatibility update below).
- The real publisher account `tlg_bridge` was created and its credentials stored
  encrypted, with the encryption key outside the database mount.
- The isolated Compose project `kobby-telegram-bot` runs on `164.68.127.58`.
- Public HTTPS `/healthz` returned 200 using the issued TLS certificate.
- Two dedicated acceptance-test users completed real Kobby automatic connection,
  including code exchange, source resolution, channel creation/reuse, and joining.
- Both received the same `tlg_alexavni` mirror:
  `f18166732fee43edaae3f193c377a9c2` (Telegram channel ID `1256713003`).
- The second acceptance run followed recreation of only the bridge container,
  verifying that the persisted registry and credentials remained usable.
- Each acceptance run sent a test publication in its own disposable Kobby channel
  and repeated the request; the server returned the same message with duplicate
  confirmation. The test bots and disposable channels were removed, and test users
  left the shared mirror. The test accounts remain because Kobby has no account
  deletion API. No real users' personal chats were used.
- All nine pre-existing containers retained the same IDs and start timestamps.
  Existing Caddy received a validated configuration reload, not a restart.

The Kobby publication contract was tested live in isolated channels. Telegram
event intake, recovery and formatting are covered by signed fixtures in the
offline suite; these checks do not claim an observed new live Telegram post.
Upstream five-post retention prevents a guarantee of complete catch-up after a
long outage, regardless of these successful tests.

## Additional-permission compatibility update

Validated locally on 2026-10-13:

- All 21 offline tests passed.
- Connections with all current Kobby permissions, selected chats, or all-personal
  chat access share the same mirror and complete successfully.
- Missing or revoked `communities.join` still prevents joining, even when other
  permissions remain. Signed identity and scope mismatches remain rejected.
- Extra message webhooks are authenticated and discarded without database writes
  or chat API calls; invalid signatures, owners, bots, paths, and timestamps fail.
- Test publication still uses the publisher user session. Subscriber tokens only
  call `/bot/me` and `/bot/communities/join` after the connection exchange.

The live acceptance script accepts repeatable `--extra-permission` arguments to
exercise this behavior with a dedicated test account and no personal chat access.
