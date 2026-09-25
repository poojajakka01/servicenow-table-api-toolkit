# ServiceNow Table API Toolkit

A small, production-minded Python client for the ServiceNow **Table API** and **Import Set API**. It handles the failure modes that break naive integration scripts: pagination drift, HTTP 429 rate limiting, transient 5xx errors, expired OAuth tokens, and accidental writes.

> **Portfolio project.** This is independently written and tested against mocked responses. It contains no employer or client code, endpoints, or credentials.

## Business problem

Most ServiceNow integrations (asset feeds, HR provisioning, monitoring tools, CMDB exports) start as a quick script and then fail in production: an instance hits its rate limit, a token expires mid-run, offset pagination skips records while data changes, or a test run writes to the wrong table. This toolkit packages the defensive patterns once, so every integration gets them.

## Features

| Capability | How |
|---|---|
| Auth | OAuth2 client credentials or password grant (token cached and refreshed before expiry, plus one forced refresh on a 401), or basic auth for a least-privilege integration user |
| Pagination | `sysparm_limit`/`sysparm_offset` with a forced `ORDERBYsys_id`, so page boundaries stay stable |
| Resilience | Exponential backoff on connection errors and 429/5xx. Honors `Retry-After`. Configurable max retries and timeout. |
| Safe writes | `dry_run=True` logs intended POST/PATCH/import calls without sending them |
| CMDB-friendly loads | `import_rows()` posts to an Import Set staging table, so transform maps and IRE handle coalescing instead of direct table writes |
| Errors | `ServiceNowError` carries status, `error.message` and `error.detail` from the platform response |
| Secrets | Read from environment variables only. `Config.__repr__` redacts credentials. |

## Quick start

```bash
pip install -e ".[dev]"
pytest -q                               # 11 tests, all HTTP mocked with `responses`

cp .env.example .env                    # point at YOUR personal developer instance
set -a; source .env; set +a
python -m snow_toolkit.export_cmdb --class cmdb_ci_server --limit 200 --out reports/
```

```python
from snow_toolkit import Config, ServiceNowClient

client = ServiceNowClient(Config.from_env(), dry_run=True)
for inc in client.iter_records("incident", "active=true^priority=1", ["number", "short_description"]):
    print(inc["number"], inc["short_description"])

client.import_rows("u_imp_server", [{"u_name": "demo-srv-001", "u_serial": "SN-DEMO-00001"}])
```

The export writes `reports/cis.json` and `reports/rels.json` in the format that the companion repo **servicenow-cmdb-health-analyzer** reads.

## Security considerations

- Use an integration account with only the roles it needs (for example `itil` read, or a custom role scoped to the staging table), never an admin account.
- Prefer OAuth over basic auth, and rotate client secrets.
- `.env` is git-ignored. CI uses mocked HTTP only and never needs real credentials.
- `Config.from_env()` rejects non-HTTPS instance URLs.

## Limitations and next steps

- Offset pagination is fine for exports. For very large, fast-changing tables, a `sys_updated_on` watermark (incremental sync) is better. Planned.
- Batch API (`/api/now/v1/batch`) support is planned for high-volume writes.
- No async support. It's intentionally synchronous and simple.

## License

MIT
