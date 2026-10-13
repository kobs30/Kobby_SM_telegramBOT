# Deployment and operations

## Production layout

- Host: `164.68.127.58` (`cyclonetestnet1`).
- Project directory: `/opt/kobby-telegram-bot`.
- Compose project: `kobby-telegram-bot`.
- HTTPS origin: `https://tlg-bridge.164-68-127-58.sslip.io`.
- User webhook: `/t.me/<Telegram username>`.
- Watcher webhook: `/events/telegram/<numeric channel ID>`.
- Publisher: `tlg_bridge` on `https://kobby.158-220-119-89.sslip.io`.

The bridge joins the existing `tg-watcher_default` network as `kobby-tlg-bridge`.
It publishes no host port. Existing Caddy routes the new hostname to port 9080.
Only the bridge is recreated during updates; Caddy configuration is reloaded
without restarting the other containers.

Runtime files (excluded from Git and image build context):

```text
runtime/config.json
runtime/data/state.sqlite
runtime/data/state.sqlite-wal
runtime/data/state.sqlite-shm
runtime/secrets/master.key
```

Directories are mode 0700 and private files 0600, owned by UID/GID 10001.
Container root filesystem is read-only, capabilities are dropped, and resources
are limited to 256 MiB, 0.5 CPU, and 64 processes. Use one replica and one worker.

Example runtime configuration:

```json
{
  "kobby_base": "https://kobby.158-220-119-89.sslip.io",
  "watcher_base": "https://tg-watcher.164-68-127-58.sslip.io",
  "public_base": "https://tlg-bridge.164-68-127-58.sslip.io",
  "data_dir": "/data",
  "key_file": "/secrets/master.key",
  "poll_interval": 60
}
```

## Deploy or update

From the project directory:

```sh
docker compose -p kobby-telegram-bot -f compose.yaml build bridge
docker compose -p kobby-telegram-bot -f compose.yaml up -d --no-deps bridge
docker compose -p kobby-telegram-bot -f compose.yaml ps
docker compose -p kobby-telegram-bot -f compose.yaml logs --tail 50 bridge
```

Do not run Docker-wide cleanup, `down -v`, or commands against other Compose
projects. Preserve runtime files and never overlay them from a code archive.
To add HTTPS, append a dedicated site block to the existing Caddyfile, validate
the full resulting config, and reload Caddy. Do not replace existing sites.

```caddyfile
tlg-bridge.164-68-127-58.sslip.io {
    request_body {
        max_size 2MB
    }
    reverse_proxy kobby-tlg-bridge:9080
}
```

## Health and status

`GET /healthz` checks the local database and worker progress. It does not prove
Telegram authorization or successful delivery of every source. Source status is
shown on its `/t.me/<username>` page; operators inspect per-connection errors and
queue state with the CLI while the bridge is stopped:

```sh
docker compose -p kobby-telegram-bot stop bridge
docker compose -p kobby-telegram-bot run --rm --no-deps bridge \
  python -m bridge.cli --config /config/config.json status
docker compose -p kobby-telegram-bot up -d --no-deps bridge
```

The process lock intentionally prevents two writers. CLI `retry` reschedules
unfinished connection and pending publication tasks after a transient condition
is fixed. It does not recreate unavailable mirrors or discard failed requests.

An initial Kobby connection may briefly display 503 until its durable join job
finishes. A missing `communities.join` permission requires changing bot settings.
Bot settings changes and rotation provide new credentials automatically.

## Backup, credentials and recovery

Stop only the bridge, copy its complete data directory and master key to encrypted
backup storage, then start it again. Preserve ownership and permissions. The key
must be recoverable but access-controlled separately from routine database backups.
The key plus database grants the publisher account and registered bot permissions.

The generated publisher password is retained encrypted to renew sessions through
ordinary login. No credentials are printed by the CLI. If the password is changed
externally, repair the encrypted publisher record through a controlled maintenance
procedure; do not delete the database or automatically register another account.
Pending requests must retain their original sender, IDs, and content.

If the watcher key changes, stop the bridge and use `set-watcher-key` with a private
file on stdin; never put the key in shell arguments. Watcher Telegram reauthorization
is performed in that project's interactive login command, not in this bot.

If a mirror is deleted, or its account loses rights, inspect the source and its
saved channel ID. Restore rights when possible. A replacement mirror requires an
explicit operator migration; this version does not automate that migration.

The upstream five-message retention remains an operational limitation even with
frequent polling. Monitor upstream health, source errors, queue age, and free disk.
Accepted snapshots/outbox rows currently have no automatic retention cleanup;
removing deduplication records requires a deliberate archival policy.
