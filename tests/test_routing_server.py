"""routing_server.py 유닛 테스트 — 가짜 ctx(query_params 지정)로 tool을 직접
호출하는 패턴(vendor/namu-agent의 test_mcp_via.py 참고).

routing_server는 import 시점에 실제 데이터를 건드리지 않으므로(코어 모듈
config/db/profile 자체가 import 시 side-effect 없음 — mcp_server.py의
`_ensure_db()` 같은 부팅 로직은 미러링하지 않았다), in-process import로 충분하다.
매 테스트는 `NAMU_STORE_ROOT`를 tmp_path 하위로 monkeypatch해 STORE_ROOT를 격리한다.
"""
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import routing_server as rs


class _FakeRequest:
    def __init__(self, query_params: dict):
        self.query_params = query_params


class _FakeRequestContext:
    def __init__(self, request):
        self.request = request


class _FakeCtx:
    def __init__(self, query_params: dict):
        self.request_context = _FakeRequestContext(_FakeRequest(query_params))


def _ctx(user: str | None = None, client: str | None = "claude") -> _FakeCtx:
    # 공용 서버는 개인용을 미러해 ?user=(라우팅)와 ?client=(출처/via)를 함께 받는다.
    # 정상 경로 테스트가 항상 유효한 client를 싣도록 기본값 "claude"를 준다.
    params: dict = {}
    if user is not None:
        params["user"] = user
    if client is not None:
        params["client"] = client
    return _FakeCtx(params)


@pytest.fixture(autouse=True)
def _store_root(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# 왕복: record → recall (같은 사용자)
# ---------------------------------------------------------------------------
def test_record_then_recall_round_trip(tmp_path):
    entry_id = rs.namu_record(
        task="구현 작업", outcome="success", reason="테스트라 성공",
        ctx=_ctx("alice"),
    )
    assert isinstance(entry_id, str) and entry_id

    result = rs.namu_recall(ctx=_ctx("alice"))
    assert "profile" in result and "learnings" in result
    ids = [d["id"] for d in result["learnings"]]
    assert entry_id in ids

    # 실제로 users/alice/ 아래에 물리적으로 남았는지 확인
    yaml_path = tmp_path / "users" / "alice" / "memory" / "learnings.yaml"
    db_path = tmp_path / "users" / "alice" / "db" / "namu.db"
    assert yaml_path.exists()
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM learnings WHERE id = ?", (entry_id,)
        ).fetchone()
    assert row == (entry_id,)


def test_search_finds_recorded_entry():
    rs.namu_record(
        task="검색용 작업", outcome="success", reason="search로 찾을 이유",
        ctx=_ctx("alice"),
    )
    result = rs.namu_search("검색용", ctx=_ctx("alice"))
    assert result["results"], "search가 방금 기록한 항목을 찾지 못함"
    assert any("검색용" in r["task"] for r in result["results"])


# ---------------------------------------------------------------------------
# 두 사용자 완전 격리
# ---------------------------------------------------------------------------
def test_two_users_fully_isolated(tmp_path):
    id_alice = rs.namu_record(
        task="alice 작업", outcome="success", reason="alice 이유",
        ctx=_ctx("alice"),
    )
    id_bob = rs.namu_record(
        task="bob 작업", outcome="failure", reason="bob 이유",
        ctx=_ctx("bob"),
    )

    recall_alice = rs.namu_recall(ctx=_ctx("alice"))
    recall_bob = rs.namu_recall(ctx=_ctx("bob"))

    alice_ids = {d["id"] for d in recall_alice["learnings"]}
    bob_ids = {d["id"] for d in recall_bob["learnings"]}

    assert id_alice in alice_ids
    assert id_alice not in bob_ids
    assert id_bob in bob_ids
    assert id_bob not in alice_ids

    # 물리 경로도 서로 다르고 서로 침범 안 함
    alice_yaml = tmp_path / "users" / "alice" / "memory" / "learnings.yaml"
    bob_yaml = tmp_path / "users" / "bob" / "memory" / "learnings.yaml"
    assert alice_yaml.exists() and bob_yaml.exists()
    assert "bob 작업" not in alice_yaml.read_text(encoding="utf-8")
    assert "alice 작업" not in bob_yaml.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 키 없음/빈 값 거부
# ---------------------------------------------------------------------------
def test_missing_user_key_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=_ctx(None))


def test_empty_user_key_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=_ctx(""))


def test_no_ctx_at_all_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=None)


def test_record_missing_user_key_rejected(tmp_path):
    with pytest.raises(ValueError):
        rs.namu_record(
            task="t", outcome="success", reason="r", ctx=_ctx(None),
        )
    # STORE_ROOT/users 자체가 생기지 않았어야 함
    assert not (tmp_path / "users").exists()


# ---------------------------------------------------------------------------
# 출처(client/via) — 개인용 미러: ?client= 없거나 형식 틀리면 거부, 있으면 저장
# ---------------------------------------------------------------------------
def test_missing_client_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=_ctx("alice", client=None))


def test_invalid_client_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=_ctx("alice", client="bad name!"))


def test_record_missing_client_rejected(tmp_path):
    with pytest.raises(ValueError):
        rs.namu_record(
            task="t", outcome="success", reason="r",
            ctx=_ctx("alice", client=None),
        )


def test_via_stored_on_record(tmp_path):
    # ?client=gemini 로 기록하면 그 항목의 via 컬럼에 'gemini'가 저장돼야 한다.
    # 이 검증이 없으면 공용 서버가 AI 출처를 흘리는 회귀(개인용 미러 누락)가 재발한다.
    entry_id = rs.namu_record(
        task="출처 저장 작업", outcome="success", reason="via 저장 확인",
        ctx=_ctx("alice", client="gemini"),
    )
    db_path = tmp_path / "users" / "alice" / "db" / "namu.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT via FROM learnings WHERE id = ?", (entry_id,)
        ).fetchone()
    assert row == ("gemini",)


# ---------------------------------------------------------------------------
# 불안전 키 거부 — 경로 이탈 방지 (핵심 보안 경계)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_key",
    [
        "../etc",
        "a/b",
        "/etc/passwd",
        "..",
        "a\\b",
        "a b",  # 공백도 허용 슬러그 밖
        "a" * 100,  # 길이 초과
    ],
)
def test_unsafe_user_key_rejected(tmp_path, bad_key):
    with pytest.raises(ValueError):
        rs.namu_record(
            task="t", outcome="success", reason="r", ctx=_ctx(bad_key),
        )
    # STORE_ROOT/users 밖에 아무 파일도 생기지 않았어야 함
    users_root = tmp_path / "users"
    if users_root.exists():
        for path in users_root.rglob("*"):
            resolved = path.resolve()
            assert str(resolved).startswith(str(users_root.resolve()))
    # STORE_ROOT 밖(예: 상위 디렉토리)에도 새 파일이 생기지 않았어야 함
    assert not (tmp_path.parent / "etc").exists()


def test_unsafe_user_key_recall_also_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=_ctx("../escape"))
    with pytest.raises(ValueError):
        rs.namu_search("q", ctx=_ctx("../escape"))


# ---------------------------------------------------------------------------
# kind=fact → profile.yaml 라우팅, namu_recall 두 그릇 반환
# ---------------------------------------------------------------------------
def test_fact_kind_routes_to_profile_yaml(tmp_path):
    fact_id = rs.namu_record(
        kind="fact",
        subject="alice",
        statement="한국어 선호",
        source="본인 발화",
        ctx=_ctx("alice"),
    )
    assert isinstance(fact_id, str) and fact_id

    profile_path = tmp_path / "users" / "alice" / "memory" / "profile.yaml"
    assert profile_path.exists()
    assert "한국어 선호" in profile_path.read_text(encoding="utf-8")

    # 같은 사용자의 learnings.yaml에는 안 들어감(다른 그릇)
    yaml_path = tmp_path / "users" / "alice" / "memory" / "learnings.yaml"
    if yaml_path.exists():
        assert "한국어 선호" not in yaml_path.read_text(encoding="utf-8")

    result = rs.namu_recall(ctx=_ctx("alice"))
    assert "profile" in result and "learnings" in result
    profile_ids = [d["id"] for d in result["profile"]]
    assert fact_id in profile_ids


def test_fact_kind_missing_source_rejected():
    with pytest.raises(ValueError):
        rs.namu_record(
            kind="fact", subject="alice", statement="stmt", source="",
            ctx=_ctx("alice"),
        )


# ---------------------------------------------------------------------------
# validate_settings / AuthMiddleware / _build_transport_security — 순수 로직만
# 테스트(mcp_server import 없이, vendor/namu-agent/namu-plugin/test_http_server.py
# 방식 참고). mcp_server가 아닌 routing_server 모듈 자체를 대상으로 한다.
# ---------------------------------------------------------------------------
async def _dummy_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _settings(**overrides) -> dict:
    base = {
        "token": "",
        "path_secret": "",
        "host": "127.0.0.1",
        "port": 8770,
        "pull_interval": 60.0,
        "allow_noauth": False,
        "allowed_hosts": [],
    }
    base.update(overrides)
    return base


def test_validate_settings_rejects_noauth():
    with pytest.raises(SystemExit) as exc_info:
        rs.validate_settings(_settings())
    assert exc_info.value.code == 2


def test_validate_settings_allows_explicit_noauth():
    rs.validate_settings(_settings(allow_noauth=True))  # SystemExit 없이 통과


def test_validate_settings_allows_token():
    rs.validate_settings(_settings(token="t"))  # SystemExit 없이 통과


def test_validate_settings_allows_path_secret():
    # v0.1.3 회귀 방지: token 없이 path_secret만 있어도(claude.ai 웹 호환 URL 경로
    # 인증) 기동을 허용해야 한다. 이 검사를 s["token"]만 보도록 원복하면 실패한다.
    rs.validate_settings(_settings(path_secret="s3cr3t"))  # SystemExit 없이 통과


def test_auth_middleware_x_api_key_match():
    app = rs.AuthMiddleware(_dummy_app, token="tok123")
    client = TestClient(app)
    r = client.get("/mcp", headers={"x-api-key": "tok123"})
    assert r.status_code == 200


def test_auth_middleware_x_api_key_mismatch():
    app = rs.AuthMiddleware(_dummy_app, token="tok123")
    client = TestClient(app)
    r = client.get("/mcp", headers={"x-api-key": "wrong"})
    assert r.status_code == 401


def test_auth_middleware_bearer_match():
    app = rs.AuthMiddleware(_dummy_app, token="tok123")
    client = TestClient(app)
    r = client.get("/mcp", headers={"Authorization": "Bearer tok123"})
    assert r.status_code == 200


def test_auth_middleware_no_header_rejected():
    app = rs.AuthMiddleware(_dummy_app, token="tok123")
    client = TestClient(app)
    r = client.get("/mcp")
    assert r.status_code == 401


def test_auth_middleware_no_token_configured_passes_through():
    app = rs.AuthMiddleware(_dummy_app, token="")
    client = TestClient(app)
    r = client.get("/mcp")
    assert r.status_code == 200


def test_build_transport_security_empty_returns_none():
    assert rs._build_transport_security([]) is None


def test_build_transport_security_star_disables_protection():
    settings = rs._build_transport_security(["*"])
    assert settings.enable_dns_rebinding_protection is False


def test_build_transport_security_adds_to_localhost_defaults_not_replaces():
    settings = rs._build_transport_security(["namu-cloud.onnamu.kr"])
    assert settings.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in settings.allowed_hosts
    assert "localhost:*" in settings.allowed_hosts
    assert "[::1]:*" in settings.allowed_hosts
    assert "namu-cloud.onnamu.kr" in settings.allowed_hosts


# ---------------------------------------------------------------------------
# resolve_streamable_path — path_secret(URL 경로 시크릿) 기반 헤더 없는 인증.
# claude.ai 웹 커스텀 커넥터는 임의 헤더(x-api-key)를 못 넣고 URL만 받으므로,
# 개인용 NAMU처럼 시크릿을 URL 경로(/mcp/<secret>)에 실어 인증한다.
# ---------------------------------------------------------------------------
def test_resolve_streamable_path_without_secret_stays_mcp():
    assert rs.resolve_streamable_path(_settings()) == "/mcp"


def test_resolve_streamable_path_with_secret_appends_it():
    assert (
        rs.resolve_streamable_path(_settings(path_secret="s3cr3t"))
        == "/mcp/s3cr3t"
    )


def test_build_app_sets_path_secret_into_streamable_http_path(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")
    monkeypatch.setenv("NAMU_HTTP_PATH_SECRET", "s3cr3t")
    rs.build_app()
    assert rs.mcp.settings.streamable_http_path == "/mcp/s3cr3t"


def test_build_app_without_path_secret_keeps_mcp(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")
    monkeypatch.delenv("NAMU_HTTP_PATH_SECRET", raising=False)
    rs.build_app()
    assert rs.mcp.settings.streamable_http_path == "/mcp"
