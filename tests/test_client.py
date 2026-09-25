import json

import pytest
import requests
import responses

from snow_toolkit.client import Config, ServiceNowClient, ServiceNowError

BASE = "https://dev00000.service-now.com"


def client(**kw):
    cfg = Config(instance_url=BASE, username="demo.user", password="not-a-real-password", page_size=2, **kw)
    return ServiceNowClient(cfg, sleep=lambda s: None)


@responses.activate
def test_pagination_stops_on_short_page():
    responses.get(f"{BASE}/api/now/table/incident", json={"result": [{"n": 1}, {"n": 2}]})
    responses.get(f"{BASE}/api/now/table/incident", json={"result": [{"n": 3}]})
    assert [r["n"] for r in client().iter_records("incident", "active=true")] == [1, 2, 3]
    assert "ORDERBYsys_id" in responses.calls[0].request.url
    assert "sysparm_offset=2" in responses.calls[1].request.url


@responses.activate
def test_limit_is_respected():
    responses.get(f"{BASE}/api/now/table/incident", json={"result": [{"n": 1}, {"n": 2}]})
    assert len(list(client().iter_records("incident", limit=1))) == 1


@responses.activate
def test_retries_on_429_with_retry_after():
    sleeps = []
    c = ServiceNowClient(Config(instance_url=BASE, username="u", password="p"), sleep=sleeps.append)
    responses.get(f"{BASE}/api/now/table/cmdb_ci/abc", status=429, headers={"Retry-After": "3"})
    responses.get(f"{BASE}/api/now/table/cmdb_ci/abc", json={"result": {"sys_id": "abc"}})
    assert c.get("cmdb_ci", "abc")["sys_id"] == "abc"
    assert sleeps == [3.0]


@responses.activate
def test_gives_up_after_max_retries():
    c = client(max_retries=2)
    for _ in range(3):
        responses.get(f"{BASE}/api/now/table/cmdb_ci/abc", status=503)
    with pytest.raises(ServiceNowError) as exc:
        c.get("cmdb_ci", "abc")
    assert exc.value.status == 503


@responses.activate
def test_error_message_is_parsed():
    responses.get(f"{BASE}/api/now/table/cmdb_ci/x", status=404,
                  json={"error": {"message": "No Record found", "detail": "check sys_id"}})
    with pytest.raises(ServiceNowError, match="No Record found"):
        client().get("cmdb_ci", "x")


@responses.activate
def test_connection_error_retried():
    c = client()
    responses.get(f"{BASE}/api/now/table/cmdb_ci/a", body=requests.ConnectionError("reset"))
    responses.get(f"{BASE}/api/now/table/cmdb_ci/a", json={"result": {"sys_id": "a"}})
    assert c.get("cmdb_ci", "a")["sys_id"] == "a"


@responses.activate
def test_oauth_client_credentials_and_refresh_on_401():
    cfg = Config(instance_url=BASE, client_id="cid", client_secret="csecret")
    c = ServiceNowClient(cfg, sleep=lambda s: None)
    responses.post(f"{BASE}/oauth_token.do", json={"access_token": "t1", "expires_in": 1800})
    responses.get(f"{BASE}/api/now/table/sys_user/1", status=401)
    responses.post(f"{BASE}/oauth_token.do", json={"access_token": "t2", "expires_in": 1800})
    responses.get(f"{BASE}/api/now/table/sys_user/1", json={"result": {"sys_id": "1"}})
    assert c.get("sys_user", "1")["sys_id"] == "1"
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer t2"


def test_dry_run_makes_no_calls():
    c = ServiceNowClient(Config(instance_url=BASE, username="u", password="p"), dry_run=True)
    assert c.create("incident", {"short_description": "x"})["sys_id"] == "dry-run"
    assert c.import_rows("u_imp_ci", [{"a": 1}])["skipped"] == 1


@responses.activate
def test_import_rows_counts_statuses():
    responses.post(f"{BASE}/api/now/import/u_imp_server", json={"result": [{"status": "inserted"}]})
    responses.post(f"{BASE}/api/now/import/u_imp_server", json={"result": [{"status": "updated"}]})
    responses.post(f"{BASE}/api/now/import/u_imp_server", status=400, json={"error": {"message": "bad"}})
    stats = client().import_rows("u_imp_server", [{"n": 1}, {"n": 2}, {"n": 3}])
    assert stats["inserted"] == 1 and stats["updated"] == 1 and stats["error"] == 1
    assert json.loads(responses.calls[0].request.body) == {"n": 1}


def test_config_from_env_requires_https(monkeypatch):
    monkeypatch.setenv("SNOW_INSTANCE_URL", "http://insecure.example")
    with pytest.raises(ValueError):
        Config.from_env()


def test_config_repr_redacts_secrets():
    cfg = Config(instance_url=BASE, username="u", password="super-secret-value")
    assert "super-secret-value" not in repr(cfg)
