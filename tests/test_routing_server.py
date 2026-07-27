"""routing_server.py 유닛 테스트 — 가짜 ctx(query_params 지정)로 tool을 직접
호출하는 패턴(vendor/namu-agent의 test_mcp_via.py 참고).

routing_server는 import 시점에 실제 데이터를 건드리지 않으므로(코어 모듈
config/db/profile 자체가 import 시 side-effect 없음 — mcp_server.py의
`_ensure_db()` 같은 부팅 로직은 미러링하지 않았다), in-process import로 충분하다.
매 테스트는 `NAMU_STORE_ROOT`를 tmp_path 하위로 monkeypatch해 STORE_ROOT를 격리한다.
"""
import asyncio
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import identity
import routing_server as rs
import user_repo as ur

# 오토유즈 스텁(_identity_and_repo_sync_stub)이 ur.ensure_ready/ur.push를 대역으로
# 바꾸기 전에 실제 함수를 붙잡아 둔다 — "미연결 사용자 거부"처럼 user_repo의
# 진짜 동작(RepoNotConnected 판정)을 겨냥하는 테스트가 이 참조로 되돌릴 수 있다.
_REAL_ENSURE_READY = ur.ensure_ready
_REAL_PUSH = ur.push


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


@pytest.fixture(autouse=True)
def _identity_and_repo_sync_stub(monkeypatch, tmp_path):
    """namu-58 4차 배선 이후 3도구는 모두 `identity.connect()` +
    `user_repo.ensure_ready`/`push`를 거친다 — 이 파일의 절대다수 테스트는
    저장소 동기화 자체가 관심사가 아니라(격리·키 검증·kind 라우팅 등 순수
    라우팅 로직 검증) "이미 로그인하고 저장소를 연결한 사용자"처럼 동작하게
    만드는 무해한 대역이 필요하다.

    - `NAMU_IDENTITY_DB_PATH`: identity.connect()가 요구하는 필수 환경변수.
      파일 기반으로 둬야 한 테스트 안의 여러 tool 호출(예: record 후 recall)이
      같은 장부를 공유한다(`:memory:`는 호출마다 새 커넥션이라 공유가 안 된다).
    - `user_repo.ensure_ready`: 진짜로 실행하면 GitHub 네트워크(github_app 토큰
      발급, git clone)가 필요하므로, 그 대신 "이미 clone된 사본이 있다"만
      흉내낸다(`.git` 폴더만 만든다) — `RepoNotConnected` 같은 진짜 연결 관문은
      전혀 검사하지 않는다.
    - `user_repo.push`: 대부분의 테스트가 push 성패에 관심이 없으므로 아무 일도
      하지 않고 "변경 없음"(False)을 돌려준다.

    이 스텁 자체(TTL 판정·미연결 거부·push 호출·push 실패 시 경고)를 검증하는
    아래 전용 테스트들은 각자 필요한 부분만 다시 monkeypatch해 실제 동작이나
    별도의 스파이(spy)로 되돌린다.
    """
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))

    def _stub_ensure_ready(conn, key):
        (ur.user_dir(key) / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(ur, "ensure_ready", _stub_ensure_ready)
    monkeypatch.setattr(
        ur, "push", lambda conn, key, message=ur.DEFAULT_COMMIT_MESSAGE: False
    )


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


def test_validate_settings_never_blocks_startup():
    """namu-59: 환경변수 인증 설정이 하나도 없어도 기동을 막지 않는다.

    예전에는 "token도 path_secret도 없으면 무인증 노출"이라 기동을 거부했다.
    지금은 모든 MCP 요청이 사용자별 열쇠 검사를 반드시 통과해야 하므로
    (열쇠 없으면 404) 무인증 노출 자체가 성립하지 않는다. 그 보장은 이 함수가
    아니라 아래 _PerUserSecretDispatcher 테스트들이 지킨다.
    """
    rs.validate_settings(_settings())  # SystemExit 없이 통과
    rs.validate_settings(_settings(allow_noauth=True))
    rs.validate_settings(_settings(token="t"))


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


def test_resolve_streamable_path_ignores_shared_path_secret():
    """namu-59: 전원 공용 열쇠(NAMU_HTTP_PATH_SECRET)는 더 이상 경로에 실리지
    않는다. 설정돼 있어도 마운트 경로는 고정이다 — 이 값을 되살려 경로에 넣으면
    "열쇠 하나를 전원이 돌려쓰는" 예전 구멍이 그대로 돌아온다."""
    assert rs.resolve_streamable_path(_settings(path_secret="s3cr3t")) == "/mcp"


def test_build_app_ignores_shared_path_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")
    monkeypatch.setenv("NAMU_HTTP_PATH_SECRET", "s3cr3t")
    rs.build_app()
    assert rs.mcp.settings.streamable_http_path == "/mcp"


def test_build_app_without_path_secret_keeps_mcp(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")
    monkeypatch.delenv("NAMU_HTTP_PATH_SECRET", raising=False)
    rs.build_app()
    assert rs.mcp.settings.streamable_http_path == "/mcp"


# ---------------------------------------------------------------------------
# _AuthOrMcpDispatcher — namu-59 2차: '/auth/'는 web_auth 앱, 그 외 전부는
# MCP+Auth 앱. 여기서는 rs.build_app()이 조립하는 실제 web_auth/FastMCP 앱이
# 아니라 대역(dummy) ASGI 콜러블 두 개로 디스패치 로직 자체만 단위 검증한다
# (web_auth 라우트 자체의 동작·보안은 tests/test_web_auth.py 소관).
# ---------------------------------------------------------------------------
def _make_labelled_app(label: str):
    async def _app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": label.encode("utf-8")})

    return _app


def test_dispatcher_routes_auth_prefix_to_auth_app():
    auth_app = _make_labelled_app("auth")
    mcp_app = _make_labelled_app("mcp")
    dispatcher = rs._AuthOrMcpDispatcher(auth_app, mcp_app)
    client = TestClient(dispatcher)
    r = client.get("/auth/github/login")
    assert r.text == "auth"


def test_dispatcher_routes_everything_else_to_mcp_app():
    auth_app = _make_labelled_app("auth")
    mcp_app = _make_labelled_app("mcp")
    dispatcher = rs._AuthOrMcpDispatcher(auth_app, mcp_app)
    client = TestClient(dispatcher)
    for path in ["/mcp", "/mcp/some-secret", "/", "/authx/notreallyauth", "/auth"]:
        r = client.get(path)
        assert r.text == "mcp", f"path={path!r}가 auth_app으로 잘못 라우팅됐다"


def test_dispatcher_default_is_the_authenticated_side():
    """디스패처 생성 시 mcp_app 자리에 실제 AuthMiddleware를 넣으면, '/auth/'가
    아닌 모든 요청이 여전히 인증을 요구해야 한다 — 기본값이 안전한 쪽인지
    회귀 방지로 한 번 더 확인(위 테스트는 대역 앱이라 인증 자체는 검증 못 함)."""
    auth_app = _make_labelled_app("auth")
    mcp_app = rs.AuthMiddleware(_dummy_app, token="tok123")
    dispatcher = rs._AuthOrMcpDispatcher(auth_app, mcp_app)
    client = TestClient(dispatcher)
    r = client.get("/mcp")
    assert r.status_code == 401
    r2 = client.get("/mcp", headers={"x-api-key": "tok123"})
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# lifespan 스모크 — TestClient를 **컨텍스트 매니저로 열어 lifespan을 실제로
# 트리거**한다(2차 검수 지적 ⑤). 위의 모든 dispatcher/build_app 테스트는
# `client.get(...)`만 호출하는데, Starlette TestClient는 컨텍스트 매니저로
# 진입하지 않으면 ASGI lifespan(startup/shutdown) 이벤트 자체를 보내지 않는다
# — 그래서 `_AuthOrMcpDispatcher.__call__`의 `scope.get("path", "")`를
# `scope["path"]`로 바꿔 lifespan scope(“path” 키가 없다)에서 KeyError가 나게
# 만들어도 그 회귀를 하나도 못 잡았다(실측: 위 테스트들은 전부 여전히 통과).
#
# 이 테스트 하나만 실제로 `with TestClient(...) as client:`를 쓴다 — FastMCP의
# StreamableHTTPSessionManager는 `routing_server.mcp`에 귀속된 모듈 싱글턴이라
# 같은 프로세스에서 lifespan을 두 번 열면(session_manager.run()이 인스턴스당
# 한 번만 허용돼) "can only be called once per instance"로 실패한다
# (vendor/namu-agent/namu-plugin/test_http_server.py 주석·실측과 동일한 제약).
# 그래서 이 파일(및 이 프로젝트 tests/ 전체)에서 lifespan을 여는 곳은 여기
# 하나뿐이어야 한다.
# ---------------------------------------------------------------------------
def test_build_app_lifespan_starts_and_stops_mcp_session_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")
    app = rs.build_app()
    # `with` 진입 자체가 lifespan startup을 보낸다 — 디스패처가 lifespan scope를
    # 처리하다 예외를 내면 이 줄에서 바로 실패한다(요청을 굳이 보낼 필요도 없다).
    with TestClient(app) as client:
        r = client.get(
            "/mcp", headers={"Accept": "application/json, text/event-stream"}
        )
        # 정확한 상태코드(호스트 헤더 검증 등 부수 조건에 따라 달라질 수 있다)를
        # 못 박기보다, "lifespan 진입 후에도 서버가 죽지 않고 요청에 응답한다"만
        # 확인한다 — 이 테스트의 목적은 프로토콜 성공이 아니라 lifespan 배선이다.
        assert r.status_code != 500
    # `with` 블록을 정상적으로 빠져나왔다는 것 자체가 shutdown도 예외 없이
    # 끝났다는 증거다.


# ---------------------------------------------------------------------------
# 저장소 동기화 배선(namu-58 4차) — TTL 판정 순수 함수
# ---------------------------------------------------------------------------
def test_needs_sync_true_when_no_local_copy():
    # user_repo.user_dir("nobody")가 가리키는 폴더 자체가 아직 없다 —
    # 사용자 결정 1의 "단, 사본이 아직 없으면 TTL과 무관하게" 조항.
    assert rs._needs_sync("nobody", ttl_sec=99999) is True


def test_needs_sync_false_within_ttl_after_mark_synced():
    key = "alice"
    (ur.user_dir(key) / ".git").mkdir(parents=True)
    rs._mark_synced(key)
    assert rs._needs_sync(key, ttl_sec=60) is False


def test_needs_sync_true_after_ttl_elapses():
    key = "alice"
    (ur.user_dir(key) / ".git").mkdir(parents=True)
    rs._mark_synced(key)
    marker = rs._sync_marker_path(key)
    # 마커 시각을 TTL보다 더 과거로 돌려 "시간이 지났다"를 흉내낸다.
    old = marker.stat().st_mtime - 120
    os.utime(marker, (old, old))
    assert rs._needs_sync(key, ttl_sec=60) is True


def test_needs_sync_true_when_local_copy_exists_but_never_marked():
    # 사본(.git)은 있지만 마커 파일이 없는 상태 — 과거(이 배선이 없던 시절)에
    # 만들어진 사본, 혹은 마커 기록 자체가 실패했던 경우를 흉내낸다. "모르면
    # 넘어가기"가 아니라 "모르면 최신화"가 안전한 기본값이어야 한다.
    key = "alice"
    (ur.user_dir(key) / ".git").mkdir(parents=True)
    assert rs._needs_sync(key, ttl_sec=99999) is True


def test_repo_sync_ttl_sec_default_is_60(monkeypatch):
    monkeypatch.delenv("NAMU_REPO_SYNC_TTL_SEC", raising=False)
    assert rs._repo_sync_ttl_sec() == 60.0


def test_repo_sync_ttl_sec_reads_env_override(monkeypatch):
    monkeypatch.setenv("NAMU_REPO_SYNC_TTL_SEC", "5")
    assert rs._repo_sync_ttl_sec() == 5.0


def test_repo_sync_ttl_sec_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("NAMU_REPO_SYNC_TTL_SEC", "not-a-number")
    with pytest.raises(ValueError):
        rs._repo_sync_ttl_sec()


# ---------------------------------------------------------------------------
# 저장소 동기화 배선 — `_ensure_repo_synced` 오케스트레이션(요구사항 ⓐⓑ)
# ---------------------------------------------------------------------------
def test_ensure_repo_synced_skips_second_call_within_ttl(monkeypatch):
    """ⓐ TTL 안에서는 다시 fetch(ensure_ready)하지 않는다."""
    calls: list[str] = []

    def _spy(conn, key):
        calls.append(key)
        (ur.user_dir(key) / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(ur, "ensure_ready", _spy)
    rs._ensure_repo_synced(None, "alice")
    rs._ensure_repo_synced(None, "alice")  # 기본 TTL(60초) 이내 — 다시 부르면 안 된다
    assert calls == ["alice"], "TTL 안에서 ensure_ready가 다시 호출됐다"


def test_ensure_repo_synced_calls_again_after_ttl_elapses(monkeypatch):
    """ⓐ TTL이 지나면 다시 fetch(ensure_ready)한다."""
    calls: list[str] = []

    def _spy(conn, key):
        calls.append(key)
        (ur.user_dir(key) / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(ur, "ensure_ready", _spy)
    monkeypatch.setenv("NAMU_REPO_SYNC_TTL_SEC", "0")  # TTL 0 — 항상 만료된 것으로 취급
    rs._ensure_repo_synced(None, "alice")
    rs._ensure_repo_synced(None, "alice")
    assert calls == ["alice", "alice"], "TTL이 지났는데도 ensure_ready가 다시 호출되지 않았다"


def test_ensure_repo_synced_always_calls_when_no_local_copy(monkeypatch):
    """ⓑ 사본이 없으면 TTL과 무관하게 매번 ensure_ready를 부른다."""
    calls: list[str] = []

    def _spy_without_creating_git(conn, key):
        # 일부러 .git을 만들지 않는다 — "사본이 여전히 없다" 상태를 매번 재현한다
        # (예: clone이 반복 실패하는 상황을 흉내).
        calls.append(key)

    monkeypatch.setattr(ur, "ensure_ready", _spy_without_creating_git)
    rs._ensure_repo_synced(None, "bob")
    rs._ensure_repo_synced(None, "bob")
    assert calls == ["bob", "bob"], "사본이 없는데도 두 번째 호출에서 ensure_ready를 건너뛰었다"


# ---------------------------------------------------------------------------
# 저장소 동기화 배선 — ⓒ 미연결 사용자는 3도구 모두 거절
# ---------------------------------------------------------------------------
def test_all_three_tools_reject_unconnected_user(monkeypatch):
    """오토유즈 스텁을 되돌려 진짜 `user_repo.ensure_ready`(RepoNotConnected 판정
    포함)가 실행되게 한다 — 이 신원 장부에는 이 user_key를 등록한 적이 없으므로
    (`identity.upsert_user`를 부른 적이 없다) 세 도구 모두 거절해야 한다."""
    monkeypatch.setattr(ur, "ensure_ready", _REAL_ENSURE_READY)

    with pytest.raises(ValueError) as exc_recall:
        rs.namu_recall(ctx=_ctx("neveronboarded"))
    with pytest.raises(ValueError) as exc_search:
        rs.namu_search("q", ctx=_ctx("neveronboarded"))
    with pytest.raises(ValueError) as exc_record:
        rs.namu_record(
            task="t", outcome="success", reason="r", ctx=_ctx("neveronboarded")
        )

    # 세 예외 모두 user_repo.RepoNotConnected의 온보딩 안내 원문(한국어+영어)을
    # 그대로 담고 있어야 한다(사용자 결정 2 — 재사용).
    for exc in (exc_recall, exc_search, exc_record):
        message = str(exc.value)
        assert "저장소를 연결" in message
        assert "GitHub App" in message


# ---------------------------------------------------------------------------
# 저장소 동기화 배선 — ⓓⓔ record 후 push 호출, push 실패해도 기록은 성공
# ---------------------------------------------------------------------------
def test_namu_record_calls_push_after_local_write(monkeypatch):
    """ⓓ 로컬 기록이 끝난 뒤 반드시 push가 호출된다."""
    calls: list[str] = []

    def _spy_push(conn, key, message=ur.DEFAULT_COMMIT_MESSAGE):
        calls.append(key)
        return True

    monkeypatch.setattr(ur, "push", _spy_push)
    entry_id = rs.namu_record(
        task="t", outcome="success", reason="r", ctx=_ctx("pusher")
    )
    assert isinstance(entry_id, str) and entry_id
    assert calls == ["pusher"], "namu_record가 로컬 기록 후 push를 부르지 않았다"


def test_namu_record_push_failure_still_succeeds_with_warning(monkeypatch, caplog):
    """ⓔ push가 실패해도 도구 호출 자체는 실패시키지 않는다 — 기록은 로컬에
    안전히 남고, 반환값에 경고가 실리며, 실패는 logger로도 남는다(사용자 결정 3)."""

    def _boom(conn, key, message=ur.DEFAULT_COMMIT_MESSAGE):
        raise ur.PushRejected("dummy push rejected for test")

    monkeypatch.setattr(ur, "push", _boom)

    with caplog.at_level("WARNING", logger="namu.routing_server"):
        result = rs.namu_record(
            task="t", outcome="success", reason="r", ctx=_ctx("pushfail")
        )

    # 반환 모양(결정 3): 성공 경로의 기존 계약(ULID 문자열)을 깨지 않기 위해,
    # push가 실패했을 때만 {"id": ..., "warning": ...} 딕셔너리로 바뀐다.
    assert isinstance(result, dict), "push 실패 시 반환값이 dict로 바뀌지 않았다"
    assert isinstance(result.get("id"), str) and result["id"]
    assert "dummy push rejected for test" in result.get("warning", "")

    # 조용히 삼키지 않는다 — 운영자가 추적할 수 있도록 logger.warning으로도 남는다.
    assert any(
        "push 실패" in rec.message for rec in caplog.records
    ), "push 실패가 logger로 전혀 남지 않았다"

    # 기록 자체는 로컬에 안전하게 남아 있어야 한다(push 경고와 무관하게).
    recall_result = rs.namu_recall(ctx=_ctx("pushfail"))
    ids = [d["id"] for d in recall_result["learnings"]]
    assert result["id"] in ids, "push가 실패했다고 로컬 기록 자체가 사라지면 안 된다"


def test_namu_record_no_warning_on_success_keeps_plain_string_return(monkeypatch):
    """push가 성공(또는 변경 없음)하면 반환값은 여전히 순수 ULID 문자열이어야
    한다 — supersedes= 등에 반환값을 그대로 넘겨 쓰는 기존 호출자와의 호환성이
    이 배선의 최우선 기준이었다."""
    monkeypatch.setattr(
        ur, "push", lambda conn, key, message=ur.DEFAULT_COMMIT_MESSAGE: True
    )
    result = rs.namu_record(
        task="t", outcome="success", reason="r", ctx=_ctx("pushok")
    )
    assert isinstance(result, str) and result


# ---------------------------------------------------------------------------
# _PerUserSecretDispatcher — 이 서버의 인증 경계 (namu-59)
#
# namu-59 이전 구조의 결함: 경로에 실린 열쇠는 전원 공용이었고, "누구인가"는
# 요청자가 스스로 적어내는 `?user=` 이름표로 정해졌다. 열쇠를 아는 사람이면
# 남의 이름표를 적어 남의 서랍을 열 수 있었다. 아래 테스트들이 그 구멍이
# 되돌아오지 않는지를 지킨다 — 특히 `_overrides_client_supplied_user`.
# ---------------------------------------------------------------------------
def _seen_scope_app():
    """통과한 scope를 붙잡아 두는 더미 앱."""
    seen: dict = {}

    async def app(scope, receive, send):
        seen["scope"] = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app, seen


def _dispatcher_get(app, path: str, query: bytes = b"") -> int:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query,
        "headers": [],
        "client": ("test", 1234),
        "server": ("testserver", 443),
        "scheme": "https",
    }
    status: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    asyncio.run(app(scope, receive, send))
    return status["code"]


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """실제 파일 장부 + 가입자 둘(alice/bob). 열쇠 두 개를 함께 돌려준다."""
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))
    with closing(identity.connect()) as conn:
        key_a = identity.upsert_user(conn, 111, "alice")
        key_b = identity.upsert_user(conn, 222, "bob")
        secret_a = identity.get_by_user_key(conn, key_a)["mcp_secret"]
        secret_b = identity.get_by_user_key(conn, key_b)["mcp_secret"]
    return {"key_a": key_a, "key_b": key_b, "secret_a": secret_a, "secret_b": secret_b}


def test_dispatcher_valid_secret_passes_and_injects_owner(ledger):
    app, seen = _seen_scope_app()
    status = _dispatcher_get(rs._PerUserSecretDispatcher(app), f"/mcp/{ledger['secret_a']}")
    assert status == 200
    assert seen["scope"]["path"] == "/mcp"  # 고정 마운트 경로로 바뀌어야 한다
    assert f"user={ledger['key_a']}" in seen["scope"]["query_string"].decode()


def test_dispatcher_overrides_client_supplied_user(ledger):
    """★핵심 회귀 방지: 요청자가 남의 이름표를 실어 보내도 열쇠 주인으로 덮인다.

    이 검사가 깨지면 namu-59가 없앤 결함(이름표만 바꿔 남의 서랍 열기)이
    그대로 되살아난다.
    """
    app, seen = _seen_scope_app()
    status = _dispatcher_get(
        rs._PerUserSecretDispatcher(app),
        f"/mcp/{ledger['secret_a']}",
        query=f"user={ledger['key_b']}&client=claude".encode(),
    )
    assert status == 200
    qs = seen["scope"]["query_string"].decode()
    assert f"user={ledger['key_a']}" in qs, "열쇠 주인으로 덮이지 않았다"
    assert ledger["key_b"] not in qs, "남의 이름표가 살아남았다"
    assert "client=claude" in qs, "다른 쿼리 항목까지 지워졌다"


def test_dispatcher_strips_every_duplicate_user_param(ledger):
    """`?user=A&user=B`처럼 여러 번 실어 보내는 수법 — 첫 항목만 고치면 뚫린다."""
    app, seen = _seen_scope_app()
    _dispatcher_get(
        rs._PerUserSecretDispatcher(app),
        f"/mcp/{ledger['secret_a']}",
        query=f"user={ledger['key_b']}&user={ledger['key_b']}".encode(),
    )
    qs = seen["scope"]["query_string"].decode()
    assert qs.count("user=") == 1
    assert ledger["key_b"] not in qs


def test_dispatcher_rejects_unknown_or_malformed_secret(ledger):
    app, _ = _seen_scope_app()
    dispatcher = rs._PerUserSecretDispatcher(app)
    for path in (
        "/mcp",                       # 열쇠 없음
        "/mcp/",                      # 빈 열쇠
        "/mcp/" + "z" * 43,           # 없는 열쇠
        "/mcp/short",                 # 형식 미달
        "/mcp/..",                    # 경로 조작
        f"/mcp/{ledger['secret_a']}/extra",   # 조각 2개
        f"/mcpx/{ledger['secret_a']}",        # 다른 경로
    ):
        assert _dispatcher_get(dispatcher, path) == 404, f"통과하면 안 되는 경로: {path}"


def test_dispatcher_fails_closed_when_ledger_unavailable(monkeypatch, ledger):
    """장부를 못 열면 통과시키지 않는다(열리는 방향으로 실패하면 전면 개방)."""
    def _boom(*args, **kwargs):
        raise RuntimeError("장부 열기 실패")

    monkeypatch.setattr(identity, "connect", _boom)
    app, _ = _seen_scope_app()
    status = _dispatcher_get(
        rs._PerUserSecretDispatcher(app), f"/mcp/{ledger['secret_a']}"
    )
    assert status == 404


def test_dispatcher_routes_each_secret_to_its_own_owner(ledger):
    """열쇠가 다르면 서랍도 달라야 한다(멀티테넌트 격리의 최종 확인)."""
    app, seen = _seen_scope_app()
    dispatcher = rs._PerUserSecretDispatcher(app)

    _dispatcher_get(dispatcher, f"/mcp/{ledger['secret_a']}")
    assert f"user={ledger['key_a']}" in seen["scope"]["query_string"].decode()

    _dispatcher_get(dispatcher, f"/mcp/{ledger['secret_b']}")
    assert f"user={ledger['key_b']}" in seen["scope"]["query_string"].decode()
