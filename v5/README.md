# ews-mcp 4.5 — Exchange (EWS) as a safe, fast MCP tool surface

An MCP server that turns an on-prem Exchange mailbox into a lean,
safety-gated tool surface for an LLM assistant: **28 tools**, alias-only
ids, token-lean DTOs, a local cache mirror with Arabic-correct full-text
search, and a two-phase confirm flow that makes autonomous sending
tamper-evident.

> The `v5/` directory name is an internal path; the release line is
> **4.5.x** (`ghcr.io/…:v4.5*`). Architecture: [DESIGN.md](DESIGN.md).
> Full API reference: [docs/API.md](docs/API.md).

## Why it looks like this

- **Token economy.** One legacy detail call shipped 115 kB of duplicated
  raw HTML for a 150-char message. Here a search result is a ~60-token
  card, bodies are cleaned once at sync time (bilingual quoted-history +
  signature stripping), and raw HTML requires an explicit flag.
- **Ids the model can actually copy.** Raw EWS ids are ~150 chars of
  case-sensitive base64 that change when items move. Tools emit short
  aliases (`m12`, `e3`) that survive moves and restarts.
- **Safety by declaration.** Handlers contain zero policy; ONE dispatcher
  chain enforces kill-switch → tier → recipient guard → content-bound
  two-phase confirm → rate cap. Defaults are safe: sends disabled,
  draft tier.
- **Cache-first reads.** A background delta-sync (native EWS
  `SyncFolderItems`) keeps a per-mailbox SQLite mirror; warm reads answer
  in milliseconds with `{"source": "cache", "as_of": …}` provenance and
  fall back to live EWS transparently. Arabic searches match across
  orthographic variants (alef/hamza forms, teh marbuta, diacritics,
  Arabic-Indic digits).
- **Never-exit boot.** Transports bind before any Exchange contact;
  `/livez` is up immediately, `/readyz` reports the warmup honestly, and
  the connection manager owns recovery.

## Quick start

```bash
pip install .                    # or: pip install -e .[dev]
export EWS_SERVER_URL="https://mail.example.com/EWS/Exchange.asmx"
export EWS_EMAIL="user@example.com"
export EWS_USERNAME="user" EWS_PASSWORD="…"
export DATA_DIR="/var/lib/ewsmcp"          # local disk, NEVER a synced folder
ewsmcp                                      # stdio MCP (Claude Desktop etc.)
MCP_TRANSPORT=http MCP_PORT=8000 ewsmcp     # HTTP: /mcp + REST + health
```

Docker:

```bash
docker build -t ews-mcp:dev .
docker run --rm -p 8000:8000 --env-file .env -v ewsmcp-data:/data ews-mcp:dev
```

## Configuration (env)

| Variable | Default | Meaning |
|---|---|---|
| `EWS_SERVER_URL` / `EWS_EMAIL` / `EWS_USERNAME` / `EWS_PASSWORD` | — | Exchange endpoint + credentials (auth auto-negotiation; never pinned) |
| `EWS_CAPABILITY_TIER` | `draft` | `read` ⊂ `draft` ⊂ `full` — above-tier tools are unregistered AND refused |
| `SEND_ENABLED` | `false` | Global send kill-switch (blocks every send-class tool) |
| `EWS_RECIPIENT_ALLOWLIST` / `EWS_RECIPIENT_DENYLIST` | — | Glob lists enforced on argument-borne AND draft-resolved recipients |
| `EWS_MAX_SENDS_PER_HOUR` | `10` | Send rate cap |
| `SEND_CONFIRM_SECRET` | per-process | HMAC secret for confirm tokens (set it to survive restarts) |
| `CONFIRM_TTL_SECONDS` | `600` | Confirm token lifetime |
| `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` / `MCP_API_KEY` | stdio | HTTP serving + bearer auth |
| `DATA_DIR` | `~/.ewsmcp` | Aliases, audit chain, cache mirror. Absolute; cloud-synced paths are refused (`DATA_DIR_ALLOW_SYNCED=true` to override) |
| `EWS_CACHE_ENABLED` | `true` | The mirror; `false` = pure live EWS reads |
| `EWS_CACHE_FOLDERS` | `inbox,sent` | Delta-synced folders |
| `EWS_CACHE_SYNC_SECONDS` | `45` | Delta cadence (folder tree/calendar/tasks every 10 min) |
| `EWS_CACHE_WINDOW_DAYS` | `365` | Mirror backfill window |
| `EWS_CACHE_PURGE_ON_BOOT` | `false` | Admin path: wipe the mirror and resync |
| `EWS_SEMANTIC_INDEX` | `none` | `pgvector` enables the optional vector tier (+`find_similar`) |
| `EWS_SEMANTIC_PG_DSN` / `EWS_SEMANTIC_OLLAMA_URL` / `EWS_SEMANTIC_MODEL` | — | Vector tier wiring (requires `psycopg`, not a core dependency) |
| `EWS_TZ` | `Asia/Riyadh` | Server timezone for date grammar + display |

## The send flow (two-phase, content-bound)

```text
create_draft(mode="reply", reply_to="m12", body="…")
  → {draft_id: "d1", preview, note: "saved as draft — NOT sent"}
send_draft(draft_id="d1")
  → phase 1: fetches the draft, returns its REAL recipients/subject/body
    snippet + confirm_token bound to that content (nothing sent)
send_draft(draft_id="d1", confirm_token="…")
  → phase 2: REFETCHES the draft, verifies the content still matches,
    sends once (tokens are single-use; editing the draft in between
    invalidates the token)
```

## Health & operations

`GET /livez` (process up), `GET /readyz` (connection state, honest 503
while warming), `GET /health` (tool count), `GET /metrics` (Prometheus,
bearer-authenticated), `get_server_status` tool (connection, tier,
kill-switch, cache watermarks, sync status — works while cold).
Audit chain: `python scripts/verify_audit_chain.py $DATA_DIR/audit`.

## Development

```bash
pip install -e .[dev]
python -m pytest tests -q          # the full suite, no Exchange needed
python -m ruff check .
python scripts/boot_smoke.py full  # end-to-end boot against a dead endpoint
python scripts/dump_tool_table.py --check   # docs vs registry drift gate
```

## Example assistant skill

`examples/skills/exchange-assistant/` shows how a Claude skill composes
this tool surface (morning overview → triage → reply-draft with the
two-phase confirm). It is deliberately generic — judgment lives in the
calling assistant, the server stays a data plane.
