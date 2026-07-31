"""사용자 신원 장부 — "누가 어느 installation·repo를 쓰는지"를 기록하는
로컬 SQLite (사용자 신원 계층 1차).

이 장부는 **기억이 아니라 서버 운영 데이터**다. 그래서 두 가지가 코어
(vendor/namu-agent)와 다르다.

1) 저장 위치: 전용 환경변수 `NAMU_IDENTITY_DB_PATH`로만 정한다.
   `NAMU_STORE_ROOT` 안쪽을 기본값으로 삼지 않는다 — 그 폴더는 사용자 기억을
   담은 **git 작업 트리**라서, 장부를 그 안에 두면 가입자 명단·GitHub
   installation id가 공용 저장소에 커밋돼 통째로 새어 나간다. 기본값을 아예
   두지 않고 미설정 시 RuntimeError를 던지는 것도 같은 이유다(실수로 아무 데나
   만들어지는 것보다 기동이 멈추는 편이 안전하다).

2) 시각 표기: `datetime.now(timezone.utc)` 기반 ISO 8601 **UTC**로 저장한다.
   namu-agent 코어는 기록 시각을 `cfg.now()`(기준 시간대 Asia/Seoul)로 찍지만,
   그건 사람이 읽는 기억 기록이기 때문이다. 이 장부는 30일 미접속 정리 같은
   기계 판정에 쓰이므로 호스트 시간대 설정과 무관하게 비교 가능해야 한다
   (컨테이너는 UTC, 개발기는 KST여도 같은 값이 나와야 한다).

커넥션 취급: 코어 db.py는 "읽기는 conn을 받고 쓰기는 내부에서 연다"는 의도된
분리를 쓰지만, identity는 그 코어가 아닌 신규 모듈이라 **읽기/쓰기 모두 conn을
인자로 받는 방식으로 일관되게** 만들었다(테스트가 `:memory:`를 주입할 수 있고,
2차 웹 라우트가 한 요청 안에서 여러 갱신을 한 트랜잭션으로 묶을 수 있다).
편의용 `connect()` 헬퍼를 따로 둔다.
"""
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# routing_server._USER_KEY_RE와 **같은 패턴이어야 한다**. import로 공유하지
# 않는 이유는 순환 참조다 — 2차에서 routing_server가 identity를 import하게
# 되므로 반대 방향 의존을 만들지 않는다. 두 정규식이 어긋나면
# tests/test_identity.py가 실패한다.
_USER_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# MCP 접속 열쇠(사용자별). token_urlsafe(32)의 출력 문자 집합과 길이(43자)를
# 그대로 받는 패턴이다. user_key와 달리 **추측 불가능해야** 하므로 짧은 값은
# 형식 검사 단계에서 거부한다 — 라우팅 서버가 경로 조각을 그대로 이 패턴에
# 물려 조회 전에 걸러낸다(DB를 두드리기 전 1차 방어선).
_MCP_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

# mcp_secret에 UNIQUE 제약을 컬럼 정의가 아니라 별도 인덱스로 거는 이유:
# SQLite의 ALTER TABLE ADD COLUMN은 UNIQUE 컬럼 추가를 허용하지 않는다. 이미
# 가입자가 들어 있는 운영 장부(namu_cloud_identity 볼륨)를 마이그레이션해야
# 하므로, 신규 생성과 기존 이관이 **같은 모양**이 되도록 양쪽 다 인덱스로 건다.
#
# `mcp_revoked_at`(namu-60)은 "이 사용자는 접속 주소를 **스스로 폐기했다**"는
# 표시다. 값이 비어 있는 것(NULL)만으로는 폐기를 표현할 수 없다 — 이 장부는
# `connect()`마다 `init_db` → `backfill_mcp_secrets`를 돌려 비어 있는 열쇠를
# 자동으로 채우므로, 폐기해서 NULL로 만든 열쇠가 바로 다음 요청에서 되살아난다
# (실측 가능: 이 칸 없이 revoke를 구현하면 재접속 한 번에 새 열쇠가 발급된다).
# 그래서 "비었다"와 "일부러 없앴다"를 칸 하나로 구분한다.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_key        TEXT PRIMARY KEY,
    github_id       INTEGER NOT NULL UNIQUE,
    login           TEXT NOT NULL,
    installation_id INTEGER,
    repo_full_name  TEXT,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    mcp_secret      TEXT,
    mcp_revoked_at  TEXT
);
"""

# 색인은 _SCHEMA와 **반드시 분리해서** 실행해야 한다. 옛 장부(mcp_secret 칸이
# 없는 표)에서는 CREATE TABLE IF NOT EXISTS가 아무것도 하지 않고 넘어가므로,
# 같은 스크립트에 색인이 붙어 있으면 "no such column: mcp_secret"으로 그 자리에서
# 깨진다 — 즉 이관해야 할 장부가 이관 시작도 못 하고 죽는다. 칸을 먼저 붙이고
# 그 다음에 색인을 건다.
_MCP_SECRET_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_mcp_secret "
    "ON users (mcp_secret) WHERE mcp_secret IS NOT NULL"
)


# ---------------------------------------------------------------------------
# 경로/커넥션
# ---------------------------------------------------------------------------
def identity_db_path() -> Path:
    """장부 파일 경로. 환경변수를 매 호출 시 읽는다(routing_server.store_root()와
    동일한 지연 평가 원칙 — 모듈 로드 시점 상수로 굳히면 테스트 격리가 안 된다).

    기본값은 의도적으로 없다(위 모듈 docstring 1번 참고).
    """
    raw = os.environ.get("NAMU_IDENTITY_DB_PATH", "").strip()
    if not raw:
        raise RuntimeError(
            "NAMU_IDENTITY_DB_PATH 환경변수가 설정되지 않았습니다 — 사용자 신원 장부를 "
            "둘 파일 경로를 지정하세요. NAMU_STORE_ROOT(사용자 기억 git 작업 트리) "
            "안쪽은 절대 지정하지 마세요(가입자 명단이 공용 저장소로 커밋됩니다). "
            "Missing NAMU_IDENTITY_DB_PATH: point it at a private volume path "
            "OUTSIDE NAMU_STORE_ROOT."
        )
    return Path(raw)


def connect(db_path: "str | Path | None" = None) -> sqlite3.Connection:
    """장부 커넥션을 연다(테이블 없으면 만든다).

    db_path를 주면 그 경로를 쓴다 — 테스트가 `:memory:`나 tmp_path를 주입하는
    통로다. 미지정이면 `identity_db_path()`.
    """
    target = str(db_path) if db_path is not None else str(identity_db_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """테이블 생성(멱등) + 기존 장부 이관.

    `CREATE TABLE IF NOT EXISTS`는 **이미 있는 표에 새 칸을 붙여주지 않는다** —
    운영 중인 장부(namu_cloud_identity 볼륨)에는 mcp_secret 칸이 없는 채로
    가입자가 들어 있으므로, 칸이 없으면 여기서 붙이고 빈 값을 채운다. 이
    이관이 없으면 기존 가입자는 접속 열쇠가 영영 NULL이라 로그인은 되는데
    MCP 주소는 못 받는 상태로 남는다.
    """
    conn.executescript(_SCHEMA)
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "mcp_secret" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN mcp_secret TEXT")
    if "mcp_revoked_at" not in existing_cols:
        # 폐기 표시 칸(namu-60). 기존 가입자는 전부 NULL = "폐기한 적 없음"이라
        # 기본 동작(비어 있으면 발급)이 그대로 유지된다.
        conn.execute("ALTER TABLE users ADD COLUMN mcp_revoked_at TEXT")
    conn.execute(_MCP_SECRET_INDEX_SQL)
    conn.commit()
    backfill_mcp_secrets(conn)


# ---------------------------------------------------------------------------
# 키/시각 유틸
# ---------------------------------------------------------------------------
def user_key_for(github_id: int) -> str:
    """사용자 식별 키 `gh-<GitHub 숫자 id>`.

    GitHub login(계정 이름)을 키로 쓰지 않는 이유가 이 함수의 존재 이유다 —
    사용자가 이름을 바꾸면 서랍이 끊기고, 버려진 이름은 타인이 재취득할 수
    있어 남의 서랍에 접근하는 결함이 된다. 숫자 id는 불변이다.
    """
    if isinstance(github_id, bool) or not isinstance(github_id, int):
        raise ValueError(
            "github_id는 정수여야 합니다. github_id must be an integer."
        )
    if github_id <= 0:
        raise ValueError(
            "github_id는 양수여야 합니다. github_id must be positive."
        )
    key = f"gh-{github_id}"
    if not _USER_KEY_RE.match(key):
        # 현실적으로 도달하지 않지만, 라우팅 서버의 사용자 키 검증을 통과하지
        # 못하는 키를 장부에 넣는 사고를 여기서 끊는다(방어선 이중화).
        raise ValueError(
            f"생성된 user_key가 허용 형식이 아닙니다: github_id={github_id}. "
            "Generated user_key does not match the allowed format."
        )
    return key


def generate_mcp_secret() -> str:
    """사용자별 MCP 접속 열쇠를 새로 만든다.

    이 값이 곧 신분증이다 — 라우팅 서버는 주소 경로에 실려 온 이 열쇠 하나로
    "누구의 서랍인가"를 판정하고, 요청자가 스스로 밝히는 이름표(`?user=`)는
    더 이상 믿지 않는다. 따라서 **추측·열거가 불가능해야** 하며, user_key
    (`gh-<GitHub 숫자 id>`, 공개 정보라 누구나 알아낼 수 있다)를 재료로 삼아선
    안 된다. token_urlsafe(32) = 256비트 난수, URL 경로에 그대로 실을 수 있는
    문자만 나온다.
    """
    return secrets.token_urlsafe(32)


def _validate_mcp_secret(mcp_secret: str) -> str:
    if (
        not isinstance(mcp_secret, str)
        or "\x00" in mcp_secret
        or not _MCP_SECRET_RE.match(mcp_secret)
    ):
        raise ValueError(
            "mcp_secret 형식이 올바르지 않습니다(영숫자·하이픈·언더스코어 32~128자). "
            "Invalid mcp_secret format."
        )
    return mcp_secret


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: "sqlite3.Row | None") -> "dict | None":
    return dict(row) if row is not None else None


def _validate_user_key(user_key: str) -> str:
    if not isinstance(user_key, str) or "\x00" in user_key or not _USER_KEY_RE.match(user_key):
        raise ValueError(
            "user_key 형식이 올바르지 않습니다(영숫자·하이픈·언더스코어 1~64자). "
            "Invalid user_key format."
        )
    return user_key


# ---------------------------------------------------------------------------
# 조회
# ---------------------------------------------------------------------------
def get_by_user_key(conn: sqlite3.Connection, user_key: str) -> "dict | None":
    row = conn.execute(
        "SELECT * FROM users WHERE user_key = ?", (user_key,)
    ).fetchone()
    return _row_to_dict(row)


def get_by_github_id(conn: sqlite3.Connection, github_id: int) -> "dict | None":
    row = conn.execute(
        "SELECT * FROM users WHERE github_id = ?", (github_id,)
    ).fetchone()
    return _row_to_dict(row)


def get_by_mcp_secret(conn: sqlite3.Connection, mcp_secret: str) -> "dict | None":
    """MCP 접속 열쇠로 사용자를 찾는다 — 라우팅 서버의 인증 진입점.

    형식이 틀린 값은 DB를 두드리지 않고 곧장 None으로 떨군다(예외를 던지지
    않는 이유: 호출부인 문지기는 "형식 오류"와 "없는 열쇠"를 구분해 알려주면
    안 된다 — 구분해 주면 공격자에게 열거 단서를 주므로 둘 다 똑같이 404다).

    빈 값/NULL 대조로 미발급 사용자가 걸려 나오는 사고를 막기 위해
    `mcp_secret IS NOT NULL` 조건을 명시한다.
    """
    try:
        _validate_mcp_secret(mcp_secret)
    except ValueError:
        return None
    row = conn.execute(
        "SELECT * FROM users WHERE mcp_secret = ? AND mcp_secret IS NOT NULL",
        (mcp_secret,),
    ).fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# 기록
# ---------------------------------------------------------------------------
def upsert_user(conn: sqlite3.Connection, github_id: int, login: str) -> str:
    """로그인 시점에 호출한다. 없으면 새로 만들고, 있으면 **login과
    last_seen_at만** 갱신한다.

    user_key는 절대 다시 계산해 덮어쓰지 않는다(github_id가 불변이므로 값 자체는
    같지만, "표시 이름이 바뀌어도 서랍은 그대로"라는 설계를 코드로 못 박는다).
    installation_id/repo_full_name도 여기서 건드리지 않는다 — 앱 설치는 별개
    단계라 로그인만 다시 했다고 연결이 풀려선 안 된다.

    mcp_secret도 같은 원칙이다 — 신규 가입 때 한 번 발급하고, 재로그인 때는
    **절대 새로 굴리지 않는다.** 매번 갈면 사용자가 클로드에 등록해 둔 커넥터
    주소가 로그인할 때마다 죽는다(재발급이 필요하면 별도 경로로 명시적으로
    한다 — `rotate_mcp_secret`). 다만 이관 등으로 비어 있으면 그때는 채운다.
    단, 사용자가 스스로 폐기한 경우(`mcp_revoked_at`)는 채우지 않는다 — 로그인
    한 번에 폐기가 취소되면 "폐기"가 아무 의미가 없다.
    """
    if not isinstance(login, str) or not login.strip():
        raise ValueError(
            "login은 비어 있을 수 없습니다. login must be a non-empty string."
        )
    key = user_key_for(github_id)
    now = _utc_now_iso()
    existing = get_by_github_id(conn, github_id)
    if existing is None:
        conn.execute(
            "INSERT INTO users (user_key, github_id, login, installation_id, "
            "repo_full_name, created_at, last_seen_at, mcp_secret) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?)",
            (key, github_id, login.strip(), now, now, generate_mcp_secret()),
        )
    else:
        conn.execute(
            "UPDATE users SET login = ?, last_seen_at = ? WHERE github_id = ?",
            (login.strip(), now, github_id),
        )
        key = existing["user_key"]
        if not existing.get("mcp_secret") and not existing.get("mcp_revoked_at"):
            conn.execute(
                "UPDATE users SET mcp_secret = ? WHERE user_key = ? AND "
                "(mcp_secret IS NULL OR mcp_secret = '') AND mcp_revoked_at IS NULL",
                (generate_mcp_secret(), key),
            )
    conn.commit()
    return key


def backfill_mcp_secrets(conn: sqlite3.Connection) -> int:
    """접속 열쇠가 비어 있는 가입자에게 발급한다. 발급한 명수를 반환(멱등).

    `init_db`가 매 접속마다 부르므로, 이 기능 이전에 가입한 사용자도 서버가
    한 번 뜨는 것만으로 열쇠를 갖게 된다. 이미 값이 있는 사람은 건드리지
    않는다 — 덮어쓰면 등록해 둔 커넥터 주소가 죽는다.

    **스스로 폐기한 사용자(`mcp_revoked_at`)도 건드리지 않는다.** 이 함수는
    `connect()`마다 불리므로, 여기서 제외하지 않으면 폐기한 열쇠가 다음 요청
    한 번에 새 값으로 되살아난다(폐기 기능이 성립하지 않는다).
    """
    rows = conn.execute(
        "SELECT user_key FROM users WHERE (mcp_secret IS NULL OR mcp_secret = '') "
        "AND mcp_revoked_at IS NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE users SET mcp_secret = ? WHERE user_key = ?",
            (generate_mcp_secret(), row["user_key"]),
        )
    if rows:
        conn.commit()
    return len(rows)


def rotate_mcp_secret(conn: sqlite3.Connection, user_key: str) -> str:
    """접속 열쇠를 새로 굴려 장부에 덮어쓰고, 새 열쇠를 돌려준다(재발급).

    **옛 주소를 따로 막는 절차가 없다는 점이 이 함수의 핵심**이다. 라우팅
    서버(`routing_server._PerUserSecretDispatcher`)는 요청마다 경로에 실린
    열쇠를 `get_by_mcp_secret`으로 장부에서 조회하고, 못 찾으면 404로 끊는다 —
    즉 장부 행을 갈아끼우는 순간 옛 열쇠는 조회에 잡히지 않아 자동으로
    죽는다(별도 블랙리스트/만료 목록을 두지 않는 이유).

    폐기 상태(`mcp_revoked_at`)에서 다시 부르면 폐기 표시를 지우고 새 열쇠를
    준다 — "폐기했다가 다시 쓰고 싶다"가 재발급 버튼 하나로 풀려야 한다.
    """
    _validate_user_key(user_key)
    new_secret = _validate_mcp_secret(generate_mcp_secret())
    cur = conn.execute(
        "UPDATE users SET mcp_secret = ?, mcp_revoked_at = NULL, last_seen_at = ? "
        "WHERE user_key = ?",
        (new_secret, _utc_now_iso(), user_key),
    )
    if cur.rowcount == 0:
        raise ValueError(
            f"등록되지 않은 user_key입니다: {user_key}. Unknown user_key."
        )
    conn.commit()
    return new_secret


def revoke_mcp_secret(conn: sqlite3.Connection, user_key: str) -> None:
    """접속 열쇠를 없앤다(폐기). 그 사용자는 어떤 주소로도 접속하지 못한다.

    빈 문자열이 아니라 NULL로 비운다 — UNIQUE 부분 색인
    (`WHERE mcp_secret IS NOT NULL`)과 `get_by_mcp_secret`의 조건이 NULL을
    전제로 쓰였고, 빈 문자열을 여럿 넣으면 두 번째 폐기에서 UNIQUE 충돌이
    난다.

    동시에 `mcp_revoked_at`을 찍는다 — 이 표시가 없으면 `backfill_mcp_secrets`
    (매 `connect()`마다 실행)가 빈 칸을 보고 곧바로 새 열쇠를 발급해 폐기가
    무효가 된다.
    """
    _validate_user_key(user_key)
    cur = conn.execute(
        "UPDATE users SET mcp_secret = NULL, mcp_revoked_at = ?, last_seen_at = ? "
        "WHERE user_key = ?",
        (_utc_now_iso(), _utc_now_iso(), user_key),
    )
    if cur.rowcount == 0:
        raise ValueError(
            f"등록되지 않은 user_key입니다: {user_key}. Unknown user_key."
        )
    conn.commit()


def set_installation(
    conn: sqlite3.Connection,
    user_key: str,
    installation_id: int,
    repo_full_name: str,
) -> None:
    """사용자가 앱을 설치하고 기억 repo를 고른 뒤 호출한다.

    등록되지 않은 user_key면 조용히 넘어가지 않고 거부한다 — 오타 하나로
    installation이 어디에도 붙지 않은 채 "설정 완료"로 보이는 상태를 막는다.
    """
    _validate_user_key(user_key)
    if isinstance(installation_id, bool) or not isinstance(installation_id, int) or installation_id <= 0:
        raise ValueError(
            "installation_id는 양의 정수여야 합니다. installation_id must be a positive integer."
        )
    if not isinstance(repo_full_name, str) or repo_full_name.count("/") != 1 or not all(
        part.strip() for part in repo_full_name.split("/")
    ):
        raise ValueError(
            "repo_full_name은 'owner/repo' 형식이어야 합니다. "
            "repo_full_name must be 'owner/repo'."
        )
    cur = conn.execute(
        "UPDATE users SET installation_id = ?, repo_full_name = ?, last_seen_at = ? "
        "WHERE user_key = ?",
        (installation_id, repo_full_name.strip(), _utc_now_iso(), user_key),
    )
    if cur.rowcount == 0:
        raise ValueError(
            f"등록되지 않은 user_key입니다: {user_key} — 먼저 upsert_user로 등록하세요. "
            "Unknown user_key: call upsert_user() first."
        )
    conn.commit()


def touch(conn: sqlite3.Connection, user_key: str) -> None:
    """접속 흔적 갱신. 30일 미접속 정리(stale_users)의 기준값을 미룬다."""
    _validate_user_key(user_key)
    cur = conn.execute(
        "UPDATE users SET last_seen_at = ? WHERE user_key = ?",
        (_utc_now_iso(), user_key),
    )
    if cur.rowcount == 0:
        raise ValueError(
            f"등록되지 않은 user_key입니다: {user_key}. Unknown user_key."
        )
    conn.commit()


# ---------------------------------------------------------------------------
# 정리 대상 조회
# ---------------------------------------------------------------------------
def stale_users(conn: sqlite3.Connection, days: int = 30) -> list[str]:
    """last_seen_at이 N일보다 오래된 user_key 목록.

    **조회만 한다** — 실제 서버 사본 삭제는 3차 user_repo.py 소관이다. 원본은
    사용자 GitHub repo에 있으므로 서버 사본은 언제든 지워도 되지만, 지우는
    행위와 고르는 행위를 한 함수에 섞으면 실수로 전량 삭제되는 사고를 막을 수
    없다(조회 결과를 사람이/호출부가 확인한 뒤 지우게 분리).

    저장 형식이 항상 같은 오프셋(+00:00)의 ISO 8601이라 문자열 비교로 시각
    비교가 성립한다.
    """
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError("days는 0 이상의 정수여야 합니다. days must be a non-negative int.")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT user_key FROM users WHERE last_seen_at < ? ORDER BY last_seen_at",
        (cutoff,),
    ).fetchall()
    return [row["user_key"] for row in rows]
