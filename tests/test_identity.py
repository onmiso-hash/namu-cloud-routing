"""identity.py 유닛 테스트 — 신원 장부(로컬 SQLite).

커넥션은 `:memory:`를 주입해 격리한다(identity가 읽기/쓰기 모두 conn을 인자로
받게 설계한 이유가 이것이다). 경로 관련 테스트만 환경변수를 monkeypatch한다.
"""
from datetime import datetime, timedelta, timezone

import pytest

import identity
import routing_server as rs


@pytest.fixture
def conn():
    c = identity.connect(":memory:")
    yield c
    c.close()


def _shift_last_seen(conn, user_key: str, days_ago: int) -> None:
    """last_seen_at을 N일 전으로 직접 밀어 넣는다(경계값 테스트용)."""
    moment = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn.execute(
        "UPDATE users SET last_seen_at = ? WHERE user_key = ?", (moment, user_key)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# user_key 규칙 — 이 설계의 핵심
# ---------------------------------------------------------------------------
def test_user_key_format_matches_real_github_id():
    assert identity.user_key_for(261774889) == "gh-261774889"


def test_user_key_passes_routing_server_validation():
    key = identity.user_key_for(261774889)
    # 라우팅 서버가 실제로 쓰는 검증기를 그대로 통과해야 한다 — 통과 못 하면
    # 로그인은 되는데 MCP 라우팅에서 거부되는 상태가 된다.
    assert rs._validate_user_key(key) == key


def test_local_user_key_regex_matches_routing_server():
    # identity는 순환 import를 피하려 정규식을 복제해 둔다. 두 패턴이 어긋나면
    # 여기서 잡는다.
    assert identity._USER_KEY_RE.pattern == rs._USER_KEY_RE.pattern


@pytest.mark.parametrize("bad", [0, -5, "123", None, True])
def test_user_key_for_rejects_bad_github_id(bad):
    with pytest.raises(ValueError):
        identity.user_key_for(bad)


def test_user_key_survives_login_rename(conn):
    """이 설계의 핵심: 계정 이름을 바꿔도 서랍(user_key)은 그대로다."""
    key_before = identity.upsert_user(conn, 261774889, "onmiso")
    identity.set_installation(conn, key_before, 5555, "onmiso/namu-memory")

    key_after = identity.upsert_user(conn, 261774889, "renamed-user")
    assert key_after == key_before == "gh-261774889"

    row = identity.get_by_user_key(conn, key_after)
    assert row["login"] == "renamed-user"       # 표시용 이름만 갱신
    assert row["installation_id"] == 5555        # 연결은 유지
    assert row["repo_full_name"] == "onmiso/namu-memory"


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------
def test_upsert_twice_keeps_single_row(conn):
    identity.upsert_user(conn, 42, "alice")
    identity.upsert_user(conn, 42, "alice")
    rows = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    assert rows["n"] == 1


def test_upsert_creates_row_with_null_installation(conn):
    key = identity.upsert_user(conn, 42, "alice")
    row = identity.get_by_user_key(conn, key)
    assert row["github_id"] == 42
    assert row["login"] == "alice"
    assert row["installation_id"] is None
    assert row["repo_full_name"] is None
    assert row["created_at"] and row["last_seen_at"]


def test_upsert_preserves_created_at(conn):
    key = identity.upsert_user(conn, 42, "alice")
    created = identity.get_by_user_key(conn, key)["created_at"]
    identity.upsert_user(conn, 42, "alice2")
    assert identity.get_by_user_key(conn, key)["created_at"] == created


def test_upsert_rejects_empty_login(conn):
    with pytest.raises(ValueError):
        identity.upsert_user(conn, 42, "   ")


def test_timestamps_are_utc_iso(conn):
    key = identity.upsert_user(conn, 42, "alice")
    row = identity.get_by_user_key(conn, key)
    parsed = datetime.fromisoformat(row["created_at"])
    # 운영 장부는 호스트 시간대와 무관하게 UTC로 저장된다.
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# installation 왕복
# ---------------------------------------------------------------------------
def test_set_installation_round_trip(conn):
    key = identity.upsert_user(conn, 7, "bob")
    identity.set_installation(conn, key, 98765, "bob/my-memory")

    by_key = identity.get_by_user_key(conn, key)
    by_id = identity.get_by_github_id(conn, 7)
    assert by_key == by_id
    assert by_key["installation_id"] == 98765
    assert by_key["repo_full_name"] == "bob/my-memory"


def test_set_installation_unknown_user_rejected(conn):
    with pytest.raises(ValueError):
        identity.set_installation(conn, "gh-999", 1, "x/y")


@pytest.mark.parametrize("bad_repo", ["norepo", "a/b/c", "/repo", "owner/", ""])
def test_set_installation_rejects_bad_repo_name(conn, bad_repo):
    key = identity.upsert_user(conn, 7, "bob")
    with pytest.raises(ValueError):
        identity.set_installation(conn, key, 1, bad_repo)


@pytest.mark.parametrize("bad_iid", [0, -1, "1", None])
def test_set_installation_rejects_bad_installation_id(conn, bad_iid):
    key = identity.upsert_user(conn, 7, "bob")
    with pytest.raises(ValueError):
        identity.set_installation(conn, key, bad_iid, "bob/repo")


def test_get_missing_returns_none(conn):
    assert identity.get_by_user_key(conn, "gh-404") is None
    assert identity.get_by_github_id(conn, 404) is None


# ---------------------------------------------------------------------------
# touch / stale_users
# ---------------------------------------------------------------------------
def test_touch_updates_last_seen(conn):
    key = identity.upsert_user(conn, 7, "bob")
    _shift_last_seen(conn, key, days_ago=100)
    assert identity.stale_users(conn) == [key]
    identity.touch(conn, key)
    assert identity.stale_users(conn) == []


def test_touch_unknown_user_rejected(conn):
    with pytest.raises(ValueError):
        identity.touch(conn, "gh-404")


def test_stale_users_boundary_29_and_31_days(conn):
    fresh = identity.upsert_user(conn, 1, "fresh")
    stale = identity.upsert_user(conn, 2, "stale")
    _shift_last_seen(conn, fresh, days_ago=29)
    _shift_last_seen(conn, stale, days_ago=31)

    result = identity.stale_users(conn, days=30)
    assert stale in result
    assert fresh not in result


def test_stale_users_custom_window(conn):
    key = identity.upsert_user(conn, 1, "u")
    _shift_last_seen(conn, key, days_ago=10)
    assert identity.stale_users(conn, days=7) == [key]
    assert identity.stale_users(conn, days=14) == []


def test_stale_users_only_reads(conn):
    key = identity.upsert_user(conn, 1, "u")
    _shift_last_seen(conn, key, days_ago=100)
    identity.stale_users(conn)
    # 조회만 한다 — 삭제는 3차 user_repo.py 소관.
    assert identity.get_by_user_key(conn, key) is not None


@pytest.mark.parametrize("bad_days", [-1, "30", None])
def test_stale_users_rejects_bad_days(conn, bad_days):
    with pytest.raises(ValueError):
        identity.stale_users(conn, days=bad_days)


# ---------------------------------------------------------------------------
# DB 경로 — 회귀 방지(가입자 명단이 공용 git 트리로 새어 나가지 않게)
# ---------------------------------------------------------------------------
def test_identity_db_path_requires_env(monkeypatch):
    monkeypatch.delenv("NAMU_IDENTITY_DB_PATH", raising=False)
    with pytest.raises(RuntimeError) as exc:
        identity.identity_db_path()
    assert "NAMU_IDENTITY_DB_PATH" in str(exc.value)


def test_identity_db_path_read_lazily(monkeypatch, tmp_path):
    target = tmp_path / "ops" / "identity.db"
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(target))
    assert identity.identity_db_path() == target


def test_identity_db_never_defaults_under_store_root(monkeypatch, tmp_path):
    """STORE_ROOT만 설정돼 있어도 장부 경로가 그 아래로 떨어지면 안 된다.

    STORE_ROOT는 사용자 기억 git 작업 트리라, 장부가 그 안에 생기면 가입자
    명단·installation id가 공용 저장소에 커밋된다. 기본값 자체가 없어야 정상.
    """
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.delenv("NAMU_IDENTITY_DB_PATH", raising=False)
    with pytest.raises(RuntimeError):
        identity.identity_db_path()
    with pytest.raises(RuntimeError):
        identity.connect()
    assert list(tmp_path.rglob("*")) == [], "STORE_ROOT 아래에 파일이 생겼다"


def test_connect_creates_parent_dirs_and_persists(monkeypatch, tmp_path):
    target = tmp_path / "ops" / "nested" / "identity.db"
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(target))
    c = identity.connect()
    try:
        key = identity.upsert_user(c, 261774889, "onmiso")
    finally:
        c.close()
    assert target.exists()

    # 재접속해도 남아 있어야 한다(init_db 멱등성 포함).
    c2 = identity.connect()
    try:
        assert identity.get_by_user_key(c2, key)["login"] == "onmiso"
    finally:
        c2.close()
