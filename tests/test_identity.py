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


# ---------------------------------------------------------------------------
# mcp_secret — 사용자별 MCP 접속 열쇠 (namu-59)
#
# 이 열쇠가 곧 신분증이다. 지켜야 할 성질이 넷 있고 아래 테스트가 하나씩 맡는다.
#   ① 가입하면 반드시 발급된다(없으면 주소를 못 받는다)
#   ② 사람마다 다르고 추측 불가능하다
#   ③ 재로그인해도 안 바뀐다(바뀌면 등록해 둔 커넥터가 죽는다)
#   ④ 옛 장부에도 소급 발급된다(이 기능 이전 가입자가 영영 못 쓰게 되면 안 된다)
# ---------------------------------------------------------------------------
def test_signup_issues_mcp_secret(conn):
    key = identity.upsert_user(conn, 261774889, "onmiso")
    row = identity.get_by_user_key(conn, key)
    assert row["mcp_secret"], "가입했는데 접속 열쇠가 발급되지 않았다"


def test_mcp_secret_is_long_and_url_safe(conn):
    key = identity.upsert_user(conn, 261774889, "onmiso")
    secret = identity.get_by_user_key(conn, key)["mcp_secret"]
    # 길이가 짧으면 추측·열거가 가능해진다. URL 경로에 그대로 실리므로
    # 슬래시·물음표 같은 문자가 섞이면 라우팅이 깨진다.
    assert len(secret) >= 32
    assert identity._MCP_SECRET_RE.match(secret)


def test_mcp_secret_is_not_derived_from_public_user_key(conn):
    """user_key(gh-<GitHub 숫자 id>)는 공개 정보다 — 열쇠가 그것을 재료로
    만들어졌다면 누구나 남의 열쇠를 계산해낼 수 있다."""
    key = identity.upsert_user(conn, 261774889, "onmiso")
    secret = identity.get_by_user_key(conn, key)["mcp_secret"]
    assert "261774889" not in secret
    assert key not in secret


def test_two_users_get_different_secrets(conn):
    key_a = identity.upsert_user(conn, 111, "alice")
    key_b = identity.upsert_user(conn, 222, "bob")
    secret_a = identity.get_by_user_key(conn, key_a)["mcp_secret"]
    secret_b = identity.get_by_user_key(conn, key_b)["mcp_secret"]
    assert secret_a != secret_b


def test_relogin_does_not_rotate_secret(conn):
    """재로그인 때 열쇠를 새로 굴리면 사용자가 클로드에 등록해 둔 커넥터
    주소가 조용히 죽는다 — 이 프로젝트에서 가장 티 안 나는 회귀 후보다."""
    key = identity.upsert_user(conn, 261774889, "onmiso")
    first = identity.get_by_user_key(conn, key)["mcp_secret"]
    identity.upsert_user(conn, 261774889, "onmiso-renamed")
    assert identity.get_by_user_key(conn, key)["mcp_secret"] == first


def test_get_by_mcp_secret_finds_the_owner(conn):
    key = identity.upsert_user(conn, 261774889, "onmiso")
    secret = identity.get_by_user_key(conn, key)["mcp_secret"]
    assert identity.get_by_mcp_secret(conn, secret)["user_key"] == key


def test_get_by_mcp_secret_rejects_unknown_and_malformed(conn):
    identity.upsert_user(conn, 261774889, "onmiso")
    assert identity.get_by_mcp_secret(conn, "a" * 43) is None       # 없는 열쇠
    assert identity.get_by_mcp_secret(conn, "short") is None        # 형식 미달
    assert identity.get_by_mcp_secret(conn, "") is None
    assert identity.get_by_mcp_secret(conn, "..") is None
    assert identity.get_by_mcp_secret(conn, "a/b" + "c" * 40) is None


def test_get_by_mcp_secret_never_matches_users_without_secret(conn):
    """빈 값/NULL 대조로 미발급 사용자가 걸려 나오면 '아무 열쇠나 통과'가 된다."""
    key = identity.upsert_user(conn, 261774889, "onmiso")
    conn.execute("UPDATE users SET mcp_secret = NULL WHERE user_key = ?", (key,))
    conn.commit()
    assert identity.get_by_mcp_secret(conn, "") is None
    assert identity.get_by_mcp_secret(conn, "x" * 43) is None


def test_legacy_ledger_without_column_is_migrated(tmp_path):
    """이 기능 이전에 만들어진 장부(mcp_secret 칸 자체가 없음)를 그대로 열면
    칸이 생기고 기존 가입자에게 열쇠가 소급 발급돼야 한다.

    운영 장부는 볼륨(namu_cloud_identity)에 남아 있으므로 이 이관이 없으면
    기존 가입자는 로그인은 되는데 주소는 영영 못 받는 상태가 된다.
    """
    import sqlite3

    target = tmp_path / "legacy.db"
    old = sqlite3.connect(str(target))
    old.executescript(
        """
        CREATE TABLE users (
            user_key        TEXT PRIMARY KEY,
            github_id       INTEGER NOT NULL UNIQUE,
            login           TEXT NOT NULL,
            installation_id INTEGER,
            repo_full_name  TEXT,
            created_at      TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL
        );
        INSERT INTO users VALUES
            ('gh-261774889', 261774889, 'onmiso', NULL, NULL, '2026-01-01', '2026-01-01');
        """
    )
    old.commit()
    old.close()

    c = identity.connect(target)
    try:
        row = identity.get_by_user_key(c, "gh-261774889")
        assert row["mcp_secret"], "옛 장부의 기존 가입자에게 열쇠가 발급되지 않았다"
        assert identity.get_by_mcp_secret(c, row["mcp_secret"])["user_key"] == "gh-261774889"
    finally:
        c.close()


def test_backfill_is_idempotent_and_preserves_existing(conn):
    key = identity.upsert_user(conn, 261774889, "onmiso")
    secret = identity.get_by_user_key(conn, key)["mcp_secret"]
    assert identity.backfill_mcp_secrets(conn) == 0  # 채울 것이 없다
    assert identity.get_by_user_key(conn, key)["mcp_secret"] == secret


# ---------------------------------------------------------------------------
# 재발급/폐기 (namu-60)
#
# 이 절의 핵심 단언은 "옛 열쇠가 **장부 조회에서** 더 이상 잡히지 않는다"이다 —
# 라우팅 서버가 요청마다 get_by_mcp_secret으로 판정하므로, 그것이 곧 "옛 주소가
# 막혔다"와 같은 말이다(실제 앱 왕복으로 한 번 더 못 박는 테스트는
# tests/test_web_auth.py에 있다).
# ---------------------------------------------------------------------------
def test_rotate_issues_new_secret_and_kills_the_old_one(conn):
    key = identity.upsert_user(conn, 261774889, "onmiso")
    old = identity.get_by_user_key(conn, key)["mcp_secret"]

    new = identity.rotate_mcp_secret(conn, key)

    assert new != old
    assert identity.get_by_user_key(conn, key)["mcp_secret"] == new
    # 옛 열쇠로는 아무도 찾을 수 없다 = 옛 주소는 404가 된다.
    assert identity.get_by_mcp_secret(conn, old) is None
    assert identity.get_by_mcp_secret(conn, new)["user_key"] == key


def test_rotate_result_is_a_valid_secret_format(conn):
    """재발급 열쇠도 신규 발급과 같은 형식이어야 한다 — 형식이 어긋나면
    get_by_mcp_secret이 DB를 두드리기도 전에 None으로 떨궈 본인도 못 쓴다."""
    key = identity.upsert_user(conn, 261774889, "onmiso")
    new = identity.rotate_mcp_secret(conn, key)
    assert 32 <= len(new) <= 128
    assert set(new) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_rotate_unknown_user_rejected(conn):
    with pytest.raises(ValueError):
        identity.rotate_mcp_secret(conn, "gh-404404")


def test_revoke_removes_the_secret_entirely(conn):
    key = identity.upsert_user(conn, 261774889, "onmiso")
    old = identity.get_by_user_key(conn, key)["mcp_secret"]

    identity.revoke_mcp_secret(conn, key)

    row = identity.get_by_user_key(conn, key)
    assert row["mcp_secret"] is None
    assert row["mcp_revoked_at"]  # 폐기 시각이 찍혔다
    assert identity.get_by_mcp_secret(conn, old) is None


def test_revoke_unknown_user_rejected(conn):
    with pytest.raises(ValueError):
        identity.revoke_mcp_secret(conn, "gh-404404")


def test_revoke_twice_does_not_collide_on_unique_index(conn):
    """두 사용자를 연달아 폐기해도 UNIQUE 색인에 걸리면 안 된다 — 빈 문자열로
    비웠다면 두 번째 폐기가 UNIQUE 충돌로 터진다(NULL로 비우는 이유)."""
    a = identity.upsert_user(conn, 111, "alice")
    b = identity.upsert_user(conn, 222, "bob")
    identity.revoke_mcp_secret(conn, a)
    identity.revoke_mcp_secret(conn, b)
    assert identity.get_by_user_key(conn, a)["mcp_secret"] is None
    assert identity.get_by_user_key(conn, b)["mcp_secret"] is None


def test_revoked_secret_is_not_resurrected_by_backfill(conn):
    """폐기의 존재 이유를 지키는 테스트.

    backfill_mcp_secrets는 `connect()`마다(init_db 경유) 돌기 때문에, "비어
    있으면 채운다"만 있고 폐기 표시가 없으면 폐기한 열쇠가 **다음 요청 한
    번에** 되살아난다. mcp_revoked_at 조건을 지우면 이 테스트가 실패한다.
    """
    key = identity.upsert_user(conn, 261774889, "onmiso")
    identity.revoke_mcp_secret(conn, key)

    assert identity.backfill_mcp_secrets(conn) == 0
    assert identity.get_by_user_key(conn, key)["mcp_secret"] is None


def test_revoked_secret_survives_reconnect(tmp_path):
    """위 테스트를 실제 파일 장부 + 재접속으로 한 번 더 — 운영에서 폐기가
    무효화되는 경로는 "다음 요청이 connect()를 다시 부른다"이므로, 그 경로를
    그대로 재현한다."""
    target = tmp_path / "identity.db"
    c = identity.connect(target)
    try:
        key = identity.upsert_user(c, 261774889, "onmiso")
        identity.revoke_mcp_secret(c, key)
    finally:
        c.close()

    c2 = identity.connect(target)  # init_db → backfill_mcp_secrets가 다시 돈다
    try:
        assert identity.get_by_user_key(c2, key)["mcp_secret"] is None
    finally:
        c2.close()


def test_relogin_does_not_resurrect_a_revoked_secret(conn):
    """폐기한 사용자가 다시 로그인해도 열쇠가 저절로 돌아오면 안 된다 —
    upsert_user의 "비어 있으면 채운다" 경로가 폐기를 취소해 버리는 사고."""
    key = identity.upsert_user(conn, 261774889, "onmiso")
    identity.revoke_mcp_secret(conn, key)

    identity.upsert_user(conn, 261774889, "onmiso")  # 재로그인

    assert identity.get_by_user_key(conn, key)["mcp_secret"] is None


def test_rotate_after_revoke_reissues_and_clears_the_revoked_mark(conn):
    """폐기했다가 마음이 바뀌면 재발급 하나로 다시 쓸 수 있어야 한다(폐기가
    영구 사망이면 사용자는 계정을 새로 만드는 수밖에 없다)."""
    key = identity.upsert_user(conn, 261774889, "onmiso")
    identity.revoke_mcp_secret(conn, key)

    new = identity.rotate_mcp_secret(conn, key)

    row = identity.get_by_user_key(conn, key)
    assert row["mcp_secret"] == new
    assert row["mcp_revoked_at"] is None
    assert identity.get_by_mcp_secret(conn, new)["user_key"] == key
    # 폐기 표시가 지워졌으니 backfill도 더는 이 사용자를 건드릴 이유가 없다.
    assert identity.backfill_mcp_secrets(conn) == 0


def test_legacy_ledger_without_revoked_column_is_migrated(tmp_path):
    """mcp_secret은 있지만 mcp_revoked_at 칸이 없는 장부(namu-59 시절 운영
    장부)를 그대로 열어도 깨지지 않고 폐기 기능이 동작해야 한다."""
    import sqlite3

    target = tmp_path / "v59.db"
    old = sqlite3.connect(str(target))
    old.executescript(
        """
        CREATE TABLE users (
            user_key        TEXT PRIMARY KEY,
            github_id       INTEGER NOT NULL UNIQUE,
            login           TEXT NOT NULL,
            installation_id INTEGER,
            repo_full_name  TEXT,
            created_at      TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            mcp_secret      TEXT
        );
        INSERT INTO users VALUES
            ('gh-261774889', 261774889, 'onmiso', 1, 'onmiso/mem',
             '2026-01-01', '2026-01-01', 'kept-secret-value-that-is-long-enough-here');
        """
    )
    old.commit()
    old.close()

    c = identity.connect(target)
    try:
        # 이관해도 기존 열쇠는 절대 갈아치우지 않는다.
        row = identity.get_by_user_key(c, "gh-261774889")
        assert row["mcp_secret"] == "kept-secret-value-that-is-long-enough-here"
        assert row["mcp_revoked_at"] is None
        identity.revoke_mcp_secret(c, "gh-261774889")
        assert identity.get_by_user_key(c, "gh-261774889")["mcp_secret"] is None
    finally:
        c.close()
