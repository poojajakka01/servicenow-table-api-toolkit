"""Resilient ServiceNow Table API / Import Set API client.

Features: OAuth2 (client credentials or password grant) or basic auth from
environment variables, keyset-friendly pagination, retry with exponential
backoff that honors Retry-After on HTTP 429, per-request timeouts, structured
logging with secrets redacted, and a dry-run mode for write operations.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ServiceNowError(RuntimeError):
    def __init__(self, status: int, message: str, detail: str = ""):
        super().__init__(f"HTTP {status}: {message} {detail}".strip())
        self.status = status
        self.message = message
        self.detail = detail


@dataclass
class Config:
    instance_url: str
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 1.0
    page_size: int = 500

    @classmethod
    def from_env(cls, prefix: str = "SNOW_") -> "Config":
        url = os.environ.get(f"{prefix}INSTANCE_URL", "").rstrip("/")
        if not url.startswith("https://"):
            raise ValueError(f"{prefix}INSTANCE_URL must be set to an https:// URL")
        return cls(
            instance_url=url,
            username=os.environ.get(f"{prefix}USERNAME"),
            password=os.environ.get(f"{prefix}PASSWORD"),
            client_id=os.environ.get(f"{prefix}CLIENT_ID"),
            client_secret=os.environ.get(f"{prefix}CLIENT_SECRET"),
        )

    def __repr__(self) -> str:  # never print secrets
        return f"Config(instance_url={self.instance_url!r}, username={self.username!r}, auth=<redacted>)"


class ServiceNowClient:
    def __init__(self, config: Config, session: requests.Session | None = None, dry_run: bool = False,
                 sleep=time.sleep):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self.dry_run = dry_run
        self._sleep = sleep
        self._token: str | None = None
        self._token_expiry = 0.0

    # ---------- auth ----------
    def _auth_headers(self) -> dict[str, str]:
        c = self.config
        if c.client_id and c.client_secret:
            if not self._token or time.time() >= self._token_expiry - 60:
                self._refresh_token()
            return {"Authorization": f"Bearer {self._token}"}
        if c.username and c.password:
            self.session.auth = (c.username, c.password)
            return {}
        raise ValueError("No credentials configured (set OAuth client or username/password)")

    def _refresh_token(self) -> None:
        c = self.config
        data = {"client_id": c.client_id, "client_secret": c.client_secret}
        if c.username and c.password:
            data.update(grant_type="password", username=c.username, password=c.password)
        else:
            data.update(grant_type="client_credentials")
        resp = self.session.post(f"{c.instance_url}/oauth_token.do", data=data, timeout=c.timeout,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
        if resp.status_code != 200:
            raise ServiceNowError(resp.status_code, "OAuth token request failed")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + float(body.get("expires_in", 1800))
        log.info("Obtained OAuth token (expires in %ss)", body.get("expires_in"))

    # ---------- transport ----------
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.config.instance_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            headers = {**kwargs.pop("headers", {}), **self._auth_headers()}
            try:
                resp = self.session.request(method, url, headers=headers, timeout=self.config.timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt > self.config.max_retries:
                    raise
                delay = self.config.backoff_base * 2 ** (attempt - 1)
                log.warning("%s %s failed (%s); retry %d in %.1fs", method, path, exc, attempt, delay)
                self._sleep(delay)
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt <= self.config.max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else \
                    self.config.backoff_base * 2 ** (attempt - 1)
                log.warning("%s %s -> %d; retry %d in %.1fs", method, path, resp.status_code, attempt, delay)
                self._sleep(delay)
                continue
            if resp.status_code == 401 and self._token and attempt == 1:
                self._token = None  # token revoked/expired early: refresh once
                continue
            if resp.status_code >= 400:
                try:
                    err = resp.json().get("error", {})
                except ValueError:
                    err = {}
                raise ServiceNowError(resp.status_code, err.get("message", resp.reason or "error"),
                                      err.get("detail", ""))
            return resp

    # ---------- Table API ----------
    def iter_records(self, table: str, query: str = "", fields: list[str] | None = None,
                     limit: int | None = None) -> Iterator[dict[str, Any]]:
        """Yield records page by page. Adds ORDERBYsys_id so offsets stay stable."""
        offset, yielded = 0, 0
        order_query = f"{query}^ORDERBYsys_id" if query else "ORDERBYsys_id"
        while True:
            params = {"sysparm_query": order_query, "sysparm_limit": self.config.page_size,
                      "sysparm_offset": offset, "sysparm_exclude_reference_link": "true"}
            if fields:
                params["sysparm_fields"] = ",".join(fields)
            batch = self._request("GET", f"/api/now/table/{table}", params=params).json().get("result", [])
            for rec in batch:
                yield rec
                yielded += 1
                if limit and yielded >= limit:
                    return
            if len(batch) < self.config.page_size:
                return
            offset += len(batch)

    def get(self, table: str, sys_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/now/table/{table}/{sys_id}").json()["result"]

    def create(self, table: str, record: dict[str, Any]) -> dict[str, Any]:
        if self.dry_run:
            log.info("[dry-run] POST %s %s", table, sorted(record))
            return {"sys_id": "dry-run", **record}
        return self._request("POST", f"/api/now/table/{table}", json=record).json()["result"]

    def update(self, table: str, sys_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        if self.dry_run:
            log.info("[dry-run] PATCH %s/%s %s", table, sys_id, sorted(changes))
            return {"sys_id": sys_id, **changes}
        return self._request("PATCH", f"/api/now/table/{table}/{sys_id}", json=changes).json()["result"]

    # ---------- Import Set API ----------
    def import_rows(self, staging_table: str, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Load rows into an import set staging table so transform maps and
        IRE handle coalescing instead of writing directly to target tables."""
        stats = {"inserted": 0, "updated": 0, "ignored": 0, "error": 0, "skipped": 0}
        for row in rows:
            if self.dry_run:
                stats["skipped"] += 1
                continue
            try:
                result = self._request("POST", f"/api/now/import/{staging_table}", json=row).json()
                status = (result.get("result") or [{}])[0].get("status", "error")
                stats[status if status in stats else "error"] += 1
            except ServiceNowError as exc:
                log.error("Import row failed: %s", exc)
                stats["error"] += 1
        return stats
