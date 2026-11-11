import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth.registry import Registry
from app.auth.scopes import ADMIN, CHILD, MEMBER, PRESETS
from app.auth.signing import build_headers
from app.config import Settings
from app.main import create_app
from tests import fake_module

CF = {"Cf-Access-Jwt-Assertion": "test-jwt"}
DOWN_TOKEN = "x" * 40


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        data_dir=tmp_path, require_cf_access=True, lockout_threshold=5,
        modules="demo=http://fake/demo,down=http://fake/down",
        module_tokens=f"demo={fake_module.TOKEN},down={DOWN_TOKEN}",
    )
    reg = Registry(settings.registry_path, settings.max_users)
    reg.add_user("dad", "아빠", ADMIN, "dad@example.com")
    reg.add_user("mom", "엄마", MEMBER, "mom@example.com")
    reg.add_user("kid", "첫째", CHILD)
    d = {
        "dad_phone": reg.add_device("dad", "phone", PRESETS["phone"]),
        "dad_watch": reg.add_device("dad", "watch", PRESETS["watch"]),
        "dad_glasses": reg.add_device("dad", "glasses", PRESETS["glasses"]),
        "mom_phone": reg.add_device("mom", "phone", PRESETS["phone"]),
        "mom_watch": reg.add_device("mom", "watch", PRESETS["watch"]),
        "kid_phone": reg.add_device("kid", "phone", PRESETS["phone"]),  # 역할 상한으로 승인 권한은 잘린다
        "dad_ping": reg.add_device("dad", "ping-only", {"demo:ping"}),
    }
    module_app, executed = fake_module.build()
    app = create_app(settings, module_transport=httpx.ASGITransport(app=module_app))
    with TestClient(app) as client:
        yield client, settings, reg, d, executed


def call(client, device, method, path, body=None, query="", headers=None):
    raw = json.dumps(body).encode() if body is not None else b""
    h = {**CF, **build_headers(device.access_key, device.secret, method, path, query, raw)}
    if body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    url = f"{path}?{query}" if query else path
    return client.request(method, url, content=raw, headers=h)


def command(client, device, action="send", module="demo", **params):
    return call(client, device, "POST", "/v1/commands", {"module": module, "action": action, "params": params})


def request_send(client, device, to="grandma@family.example"):
    return command(client, device, "send", to=to)


def pending_ids(client, device):
    r = call(client, device, "GET", "/v1/approvals", query="status=pending")
    assert r.status_code == 200
    return [a["id"] for a in r.json()["items"]]


# ── 인증 ─────────────────────────────────────────────────────

def test_healthz_is_open(env):
    client, *_ = env
    assert client.get("/healthz").json() == {"ok": True}


def test_signed_request_succeeds(env):
    client, _, _, d, _ = env
    r = call(client, d["dad_phone"], "GET", "/v1/me")
    assert r.status_code == 200
    assert r.json()["user"] == {"id": "dad", "name": "아빠", "role": "admin"}


def test_requires_cloudflare_access_header(env):
    client, _, _, d, _ = env
    h = build_headers(d["dad_phone"].access_key, d["dad_phone"].secret, "GET", "/v1/me")
    assert client.get("/v1/me", headers=h).status_code == 401


def test_bad_signature_rejected(env):
    client, _, _, d, _ = env
    h = {**CF, **build_headers(d["dad_phone"].access_key, "wrong-secret", "GET", "/v1/me")}
    assert client.get("/v1/me", headers=h).status_code == 401


def test_tampered_body_rejected(env):
    client, _, _, d, _ = env
    body = json.dumps({"module": "demo", "action": "ping", "params": {"to": "a"}}).encode()
    dev = d["dad_glasses"]
    h = {**CF, **build_headers(dev.access_key, dev.secret, "POST", "/v1/commands", "", body),
         "Content-Type": "application/json"}
    evil = json.dumps({"module": "demo", "action": "ping", "params": {"to": "evil"}}).encode()
    assert client.post("/v1/commands", content=evil, headers=h).status_code == 401


def test_replay_rejected(env):
    client, _, _, d, _ = env
    h = {**CF, **build_headers(d["dad_phone"].access_key, d["dad_phone"].secret, "GET", "/v1/me")}
    assert client.get("/v1/me", headers=h).status_code == 200
    assert client.get("/v1/me", headers=h).status_code == 401


def test_old_timestamp_rejected(env, monkeypatch):
    client, _, _, d, _ = env
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3600)
    h = {**CF, **build_headers(d["dad_phone"].access_key, d["dad_phone"].secret, "GET", "/v1/me")}
    monkeypatch.setattr(time, "time", real)
    assert client.get("/v1/me", headers=h).status_code == 401


def test_revoked_key_rejected(env):
    client, _, reg, d, _ = env
    reg.revoke(d["dad_phone"].access_key)
    assert call(client, d["dad_phone"], "GET", "/v1/me").status_code == 401


def test_lockout_after_repeated_failures(env):
    client, settings, _, d, _ = env
    for _ in range(settings.lockout_threshold):
        h = {**CF, **build_headers(d["dad_phone"].access_key, "wrong", "GET", "/v1/me")}
        client.get("/v1/me", headers=h)
    # 올바른 서명이어도 잠긴 동안은 거부
    assert call(client, d["dad_phone"], "GET", "/v1/me").status_code == 401


def test_disabled_user_devices_rejected(env):
    client, _, reg, d, _ = env
    reg.disable_user("kid")
    assert call(client, d["kid_phone"], "GET", "/v1/me").status_code == 401


# ── 권한 ─────────────────────────────────────────────────────

def test_device_scope_enforced(env):
    client, _, _, d, executed = env
    # 워치는 모듈 권한이 없다 (승인만)
    assert request_send(client, d["dad_watch"]).status_code == 403
    # demo:ping 만 받은 기기는 ping만 된다
    assert command(client, d["dad_ping"], "ping").status_code == 200
    assert request_send(client, d["dad_ping"]).status_code == 403
    assert len(executed) == 1


def test_role_caps_device_scopes(env):
    client, _, _, d, _ = env
    # 아이 폰에 phone 프리셋을 줘도, 역할 상한 때문에 승인 권한은 없다
    r = call(client, d["kid_phone"], "GET", "/v1/me")
    assert r.json()["scopes"] == ["modules:*", "system:read"]
    assert call(client, d["kid_phone"], "GET", "/v1/approvals").status_code == 403


def test_manifest_roles_enforced(env):
    client, _, _, d, executed = env
    assert command(client, d["kid_phone"], "secret").status_code == 403
    assert command(client, d["mom_phone"], "secret").status_code == 403
    assert command(client, d["dad_phone"], "secret").status_code == 200
    assert [e["action"] for e in executed] == ["secret"]


def test_unknown_module_or_action(env):
    client, _, _, d, _ = env
    assert command(client, d["dad_phone"], "ping", module="nope").status_code == 404
    assert command(client, d["dad_phone"], "nope").status_code == 404
    # 권한이 없는 기기에는 모듈이 있든 없든 똑같이 403
    assert command(client, d["dad_watch"], "ping", module="nope").status_code == 403


def test_module_down(env):
    client, _, _, d, _ = env
    assert command(client, d["dad_phone"], "ping", module="down").status_code == 503


def test_list_modules(env):
    client, _, _, d, _ = env
    items = call(client, d["kid_phone"], "GET", "/v1/modules").json()["items"]
    demo = next(i for i in items if i["name"] == "demo")
    # 아이에게는 관리자 전용 액션이 보이지 않는다
    assert [a["name"] for a in demo["actions"]] == ["send", "ping"]
    assert {"name": "down", "available": False, "actions": []} in items
    # 모듈 권한이 없는 워치에게는 모듈 목록이 비어 있다 (꺼진 모듈만 보인다)
    items = call(client, d["dad_watch"], "GET", "/v1/modules").json()["items"]
    assert [i["name"] for i in items if i["available"]] == []


# ── 명령과 승인 ──────────────────────────────────────────────

def test_command_without_approval_runs_now(env):
    client, _, _, d, executed = env
    r = command(client, d["dad_glasses"], "ping", to="x")
    assert r.status_code == 200 and r.json() == {"result": "ping 완료"}
    # 모듈에는 누가 시켰는지만 넘어가고, 기기 키나 Secret은 넘어가지 않는다
    assert executed[0]["caller"] == {"user_id": "dad", "name": "아빠", "role": "admin", "email": "dad@example.com"}
    assert d["dad_glasses"].secret not in json.dumps(executed)


def test_module_validation_error_shown(env):
    client, _, _, d, executed = env
    r = request_send(client, d["dad_glasses"], to="stranger@evil.example")
    assert r.status_code == 422
    assert r.json()["message"] == "허용되지 않은 수신자가 있습니다."
    assert executed == []


def test_approval_flow(env):
    client, _, _, d, executed = env
    r = request_send(client, d["dad_glasses"])
    assert r.status_code == 202
    approval = r.json()["approval"]
    assert approval["rule"] == "same_user" and approval["action"] == "demo.send"
    assert approval["summary"] == "아빠: send → grandma@family.example"
    assert executed == []  # 아직 실행 안 됨

    assert pending_ids(client, d["dad_watch"]) == [approval["id"]]
    r = call(client, d["dad_watch"], "GET", "/v1/approvals")
    assert "params" not in r.json()["items"][0]  # 요약만 노출

    r = call(client, d["dad_watch"], "POST", f"/v1/approvals/{approval['id']}/approve")
    assert r.status_code == 200
    assert r.json()["approval"]["status"] == "approved"
    assert r.json()["approval"]["result"] == "send 완료"
    assert len(executed) == 1
    assert executed[0]["params"] == {"to": "grandma@family.example"}
    assert executed[0]["caller"]["name"] == "아빠"

    # 두 번 승인해도 한 번만 실행
    assert call(client, d["dad_phone"], "POST", f"/v1/approvals/{approval['id']}/approve").status_code == 409
    assert len(executed) == 1


def test_failed_execution_marked_failed(env):
    client, _, _, d, _ = env
    approval_id = command(client, d["dad_glasses"], "send", to="a@family.example", boom=True).json()["approval"]["id"]
    r = call(client, d["dad_watch"], "POST", f"/v1/approvals/{approval_id}/approve")
    assert r.status_code == 502
    r = call(client, d["dad_watch"], "GET", "/v1/approvals", query="status=failed")
    assert [a["id"] for a in r.json()["items"]] == [approval_id]


def test_requester_device_cannot_self_approve(env):
    client, _, _, d, executed = env
    approval_id = request_send(client, d["dad_phone"]).json()["approval"]["id"]
    assert call(client, d["dad_phone"], "POST", f"/v1/approvals/{approval_id}/approve").status_code == 403
    assert executed == []


def test_family_cannot_see_or_approve_each_other(env):
    client, _, _, d, executed = env
    mom_req = request_send(client, d["mom_phone"]).json()["approval"]["id"]
    # 관리자인 아빠에게도 엄마의 요청은 보이지 않고, 승인하려 하면 "없는 요청"과 똑같이 404
    assert mom_req not in pending_ids(client, d["dad_watch"])
    assert call(client, d["dad_watch"], "POST", f"/v1/approvals/{mom_req}/approve").status_code == 404
    # 엄마는 자기 워치로 승인
    assert pending_ids(client, d["mom_watch"]) == [mom_req]
    assert call(client, d["mom_watch"], "POST", f"/v1/approvals/{mom_req}/approve").status_code == 200
    assert executed[0]["caller"]["name"] == "엄마"


def test_child_request_needs_admin(env):
    client, _, _, d, executed = env
    r = request_send(client, d["kid_phone"])
    assert r.status_code == 202
    kid_req = r.json()["approval"]["id"]
    assert r.json()["approval"]["rule"] == "admin"
    # 어른이어도 관리자가 아니면 보이지도, 승인되지도 않는다
    assert kid_req not in pending_ids(client, d["mom_watch"])
    assert call(client, d["mom_watch"], "POST", f"/v1/approvals/{kid_req}/approve").status_code == 404
    # 관리자는 볼 수 있고 승인할 수 있다
    assert kid_req in pending_ids(client, d["dad_watch"])
    assert call(client, d["dad_watch"], "POST", f"/v1/approvals/{kid_req}/approve").status_code == 200
    assert len(executed) == 1 and executed[0]["caller"]["name"] == "첫째"


def test_reject_flow(env):
    client, _, _, d, executed = env
    approval_id = request_send(client, d["dad_glasses"]).json()["approval"]["id"]
    r = call(client, d["dad_watch"], "POST", f"/v1/approvals/{approval_id}/reject")
    assert r.json()["approval"]["status"] == "rejected"
    assert executed == []


def test_audit_log_has_no_secrets_or_params(env):
    client, settings, _, d, _ = env
    command(client, d["dad_glasses"], "ping", to="TOP-SECRET-PARAM")
    log = settings.audit_path.read_text()
    assert "TOP-SECRET-PARAM" not in log
    for dev in d.values():
        assert dev.secret not in log
    assert '"user": "dad"' in log


# ── 이벤트 (모듈 → 허브) ─────────────────────────────────────

def post_event(client, token=fake_module.TOKEN, headers=None, **body):
    h = {"Authorization": f"Bearer {token}", **(headers or {})}
    return client.post("/internal/events", json={"type": "message.received", "title": "엄마: 저녁 뭐 먹어?", **body},
                       headers=h)


def test_module_event_reaches_user(env):
    client, _, _, d, _ = env
    assert post_event(client, user="dad").status_code == 201
    assert post_event(client, user=None, title="택배 도착").status_code == 201
    dad = call(client, d["dad_phone"], "GET", "/v1/events").json()["items"]
    assert [(e["module"], e["title"]) for e in dad] == [("demo", "엄마: 저녁 뭐 먹어?"), ("demo", "택배 도착")]
    mom = call(client, d["mom_phone"], "GET", "/v1/events").json()["items"]
    assert [e["title"] for e in mom] == ["택배 도착"]  # 아빠에게 온 이벤트는 안 보인다
    # 마지막으로 받은 id 다음 것만
    assert call(client, d["dad_phone"], "GET", "/v1/events", query=f"after={dad[0]['id']}").json()["items"] == dad[1:]


def test_internal_endpoint_rejects_bad_token_and_tunnel(env):
    client, *_ = env
    assert post_event(client, token="wrong" * 10, user="dad").status_code == 401
    # Cloudflare를 거쳐 온 요청이면 토큰이 맞아도 없는 경로처럼
    assert post_event(client, user="dad", headers={"Cf-Connecting-Ip": "203.0.113.5"}).status_code == 404
    assert post_event(client, user="ghost").status_code == 422
