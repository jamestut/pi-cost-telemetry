# pi-cost-telemetry

Track per-turn token usage and cost from [Pi](https://github.com/badlogic/pi) coding-agent sessions. A Pi extension POSTs metadata to a local ingest server, which stores it in SQLite and exposes aggregated summaries.

**What is sent:** token counts, cost breakdown, model name, provider, stop reason, timestamps. **Never sent:** prompts, completions, tool arguments, or file contents.

## Components

| File | Purpose |
|------|---------|
| `cost-telemetry.ts` | Pi extension — hooks `turn_end`, POSTs metadata to your endpoint |
| `server.py` | Ingest server — receives turns, stores in SQLite, serves `/summary` |
| `query.py` | CLI client — queries `/summary`, prints nested totals by session/model |

## Quick start

### 1. Run the ingest server

Create a config mapping API keys to session buckets:

```json
{
  "work": { "keys": ["key-work-1"] },
  "personal": { "keys": ["key-personal-1", "key-personal-2"] }
}
```

```bash
python3 server.py --config keys.json --db telemetry.db --listen-port 8000
```

### 2. Configure the Pi extension

Place `cost-telemetry.ts` in your Pi extensions directory, then create `~/.pi/agent/cost-telemetry.json`:

```json
{
  "endpoint": "http://127.0.0.1:8000/ingest",
  "apiKey": "key-work-1",
  "timeoutMs": 5000
}
```

Env overrides: `PI_COST_TELEMETRY_ENDPOINT`, `PI_COST_TELEMETRY_API_KEY`. API key precedence (highest to lowest): env var → `~/.pi/agent/auth.json` under `"cost-telemetry"` → `cost-telemetry.json` `apiKey` field.

### 3. Query usage

```bash
python3 query.py --at 2026-09
python3 query.py --since 2026-09-01 --until 2026-09-30 --session work
python3 query.py --at 2026-09-07 --tz Asia/Shanghai --json
```

Set `TELEMETRY_KEY` env var or pass `--key` to authenticate.

## Server endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/ingest` or `/` | Bearer | Ingest one turn payload |
| GET | `/summary` | Bearer | Aggregated totals by session+model |
| GET | `/health` | none | Liveness probe |

### /summary query params

| Param | Description |
|-------|-------------|
| `session` | Filter by session bucket |
| `model` | Filter by model name |
| `at` | Single period: `YYYY`, `YYYY-MM`, or `YYYY-MM-DD` |
| `since` | Range start (same granularity as `until`) |
| `until` | Range end (exclusive) |
| `tz` | IANA timezone for period boundaries (default: UTC) |

`at` cannot combine with `since`/`until`. `since` and `until` must share granularity.

## Schema

SQLite table `turns` — one row per ingested turn. Key columns: `session`, `model`, `received_at_ms`, `in_tokens`, `out_tokens`, `cache_read`, `cache_write`, `total_tokens`, `reasoning_tokens`, `cost_in`, `cost_out`, `cost_cache_read`, `cost_cache_write`, `cost_total`, `payload` (raw JSON).
