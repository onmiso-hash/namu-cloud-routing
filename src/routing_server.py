"""NAMU 공용 라우팅 MCP 서버 (namu-50 결정, stateless HTTP).

요청마다 URL 쿼리(`?user=<키>`)로 사용자를 식별해, 포터블 메모리 코어
(vendor/namu-agent/namu-plugin의 config/db/profile)를 그 사용자 전용 데이터
디렉토리(`STORE_ROOT/users/<키>/`)로 라우팅한다. 개인용 NAMU(mcp_server.py,
단일 데이터 루트 ~/.namu, stdio)와는 완전히 분리된 별도 서비스다.

코어는 복제하지 않는다 — vendor 서브모듈(태그 핀)을 sys.path로 얹어 그대로
재사용하고, 이 파일은 "데이터 루트를 요청별로 갈아끼우는" 라우팅 로직만 담는다.
개인용 mcp_server.py의 3도구(namu_record/namu_recall/namu_search) 로직을
그대로 미러링하되, 전역 경로(cfg.NAMU_DB_PATH 등) 하드코딩 대신 매 호출마다
`paths=cfg.data_paths_for(user_root)`를 코어에 넘긴다.

그릇(bowl)은 네 개 다 받는다. 교훈(learnings)·개인 사실(profile)·쪽지(memo)는
DataPaths로 갈리고, 작업일지(tasks)는 파일이 아니라 폴더 구조라 DataPaths에
자리가 없으므로 회원 폴더의 `tasks/<프로젝트>/<작업>/`에 직접 읽고 쓴다.

namu-68은 tasks를 거절했었다 — 근거는 "코어가 `Path.home()/.namu/tasks`에 쓰므로
갈아끼울 자리가 없다"였는데, 그건 코어의 한 함수(`task_resolve.tasks_root_for`)에만
해당하는 사실이었고 이 서버는 그 함수를 쓸 이유가 없었다. 나머지 코어 함수는 전부
폴더를 인자로 받는다. 자세한 경위는 아래 "작업일지" 절 주석에 있다.

보안 경계(멀티테넌트 격리의 핵심)는 `_resolve_user`/`_validate_user_key`/
`_paths_for_user` 세 함수에 있다 — 키가 없거나 안전하지 않으면(경로 이탈 문자
포함) 저장/조회를 거부하고, resolve() 후 STORE_ROOT/users 밖으로 벗어나지
않는지 이중으로 재확인한다.

`STORE_ROOT/users/<키>/` 폴더 자체는 `user_repo.py`(사용자 신원 계층 3차)가
관리하는 **사용자 GitHub 저장소의 캐시(사본)**다(user_dir()가 이 파일의
_paths_for_user()와 같은 폴더를 가리킨다 — 계약은 tests/test_user_repo.py로
고정됨). namu-58 4차 배선까지는 이 파일이 user_repo를 한 번도 부르지 않아
(grep 실측 0건) 그 사실이 무의미했다 — 3도구가 항상 이 폴더를 직접 읽고 쓰되,
그 폴더를 "지금 쓸 수 있는 최신 상태"로 만들거나(`ensure_ready`) 변경을
사용자 저장소로 되돌려 보내는(`push`) 일은 전혀 일어나지 않았다. 이 배선
(`_sync_or_reject`/`_push_and_collect_warning`, TTL 관련 함수들) 이후에는
읽기 전에 TTL 기반으로 최신화하고, 쓰기 후에 항상 push를 시도한다.
"""
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

# vendor/namu-agent/namu-plugin을 sys.path에 얹는다 (이 파일 위치 기준 절대경로).
# vendor/namu-agent는 수정 금지 대상 — 코어는 읽기 재사용만 한다.
_VENDOR_PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent / "vendor" / "namu-agent" / "namu-plugin"
)
if str(_VENDOR_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_PLUGIN_DIR))

import access_log  # noqa: E402
import config as cfg  # noqa: E402
import db  # noqa: E402
import identity  # noqa: E402
import memo  # noqa: E402
import profile  # noqa: E402
import record_input  # noqa: E402
import task_resolve  # noqa: E402
import ui  # noqa: E402
import user_repo  # noqa: E402
import web_auth  # noqa: E402
from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

# 이 서버가 내주는 도구. **소개문도 이 목록에서 만든다** — 목록과 소개문이 갈라지면
# 붙은 AI가 없는 도구를 부른다. 셀프호스팅 쪽이 실제로 그랬고(소개 7종/노출 3종),
# 클라우드는 반대로 소개문이 통째로 없어 붙은 AI가 네 그릇이 무엇인지 안내받지
# 못했다(2026-08-05 두 서버를 띄워 실측). 코어(vendor/namu-agent)의 같은 함수를
# 쓰므로 두 경로의 소개문이 한 원본에서 나온다.
EXPOSED_TOOLS = frozenset({"namu_recall", "namu_search", "namu_record"})

mcp = FastMCP(
    "namu-cloud-routing",
    instructions=record_input.server_instructions(EXPOSED_TOOLS),
)

logger = logging.getLogger("namu.routing_server")


# ---------------------------------------------------------------------------
# STORE_ROOT — 사용자 디렉토리들의 부모. 환경변수를 매 호출 시 읽는다(모듈 로드
# 시점 상수로 고정하면 테스트에서 monkeypatch.setenv로 격리하기 어렵다 —
# config.http_settings()와 동일한 지연 평가 원칙).
# ---------------------------------------------------------------------------
def store_root() -> Path:
    raw = os.environ.get("NAMU_STORE_ROOT", "").strip()
    if not raw:
        raise RuntimeError(
            "NAMU_STORE_ROOT 환경변수가 설정되지 않았습니다 — "
            "사용자 데이터가 쌓일 STORE clone 경로를 지정하세요."
        )
    return Path(raw)


# ---------------------------------------------------------------------------
# 사용자 키 추출/검증 — 멀티테넌트 격리의 보안 경계 (최우선 구현 대상)
# ---------------------------------------------------------------------------
_USER_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_USER_KEY_ERROR_MSG = (
    "사용자 키가 없거나 형식이 올바르지 않습니다 — 요청 URL에 ?user=<키>를 올바른 "
    "형식으로 붙이세요 (영숫자·하이픈·언더스코어만, 1~64자, 경로 문자 금지). "
    "Missing/invalid 'user' key: append ?user=<your-key> to the MCP URL "
    "(alphanumeric/hyphen/underscore only, 1-64 chars)."
)


def _validate_user_key(key: str) -> str:
    """안전한 슬러그(영숫자·-·_ 1~64자)만 허용한다.

    `/`, `\\`, `..`, 널바이트, 공백 등 경로 이탈에 쓰일 수 있는 문자는 정규식
    자체가 통과시키지 않는다 — 널바이트 명시 검사는 방어선 이중화(문서화 목적)다.
    """
    if not key or "\x00" in key or not _USER_KEY_RE.match(key):
        raise ValueError(_USER_KEY_ERROR_MSG)
    return key


_VIA_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")

_VIA_ERROR_MSG = (
    "출처(client) 식별값이 없거나 형식이 올바르지 않습니다 — 주소 끝에 "
    "&client=<AI 이름>을 붙이고, 사용하는 AI 이름을 정확히 넣으세요. "
    "예: claude, chatgpt, gemini, cursor, copilot. 애칭·약칭도 되지만, 나중에 "
    "'그 AI가 남긴 기억'을 조회하려면 입력했던 값과 똑같이 넣어야 찾을 수 있습니다 "
    "(claude 와 cld 는 서로 다른 값으로 저장됨).  |  Missing/invalid 'client': "
    "append &client=<your-ai-name> (e.g. claude, chatgpt, gemini). Use the exact "
    "same value later to look up that AI's memories."
)


def _resolve_via(ctx: "Context | None") -> str | None:
    """URL 쿼리(`?client=`)에서 출처(via) 태그를 읽어 검증한다 — 개인용
    mcp_server._resolve_via(namu-50)를 그대로 미러. '어느 AI가 남긴 기억인지'를
    각 기록에 함께 저장·구분하기 위한 출처 태그다.

    이 서버는 stateless HTTP 전용이라 요청 경로에선 req가 항상 존재한다 →
    `?client=`가 없거나 형식이 틀리면 거부한다(개인용의 웹 경로 동작과 동일).
    ctx/req가 없는 경우(테스트/직접 호출)만 면제하고 None을 반환한다.
    """
    if ctx is None:
        return None
    req = getattr(getattr(ctx, "request_context", None), "request", None)
    if req is None:
        return None
    client = (req.query_params.get("client") or "").strip()
    if not _VIA_RE.match(client):
        raise ValueError(_VIA_ERROR_MSG)
    return client


def _resolve_user(ctx: "Context | None") -> str:
    """URL 쿼리(`?user=`)에서 사용자 키를 읽어 검증한다.

    이 서버는 "요청마다 어느 사용자로 라우팅할지"가 존재 이유이므로, via(출처
    태그)와 달리 ctx/request가 없으면 면제하지 않고 곧바로 거부한다 — 라우팅
    대상 자체를 판별할 수 없기 때문이다.
    """
    req = getattr(getattr(ctx, "request_context", None), "request", None) if ctx is not None else None
    if req is None:
        raise ValueError(_USER_KEY_ERROR_MSG)
    raw = (req.query_params.get("user") or "").strip()
    return _validate_user_key(raw)


def _paths_for_user(key: str) -> "cfg.DataPaths":
    """검증된 키로부터 사용자 전용 DataPaths를 만든다.

    키 자체는 이미 `_validate_user_key`로 안전한 슬러그임이 보장되지만,
    STORE_ROOT/users 밖으로 벗어나지 않는지 resolve() 후 재확인한다(경로 탈출
    이중 차단 — 멀티테넌트 격리의 핵심 방어선).
    """
    users_root = (store_root() / "users").resolve()
    candidate = (users_root / key).resolve()
    try:
        candidate.relative_to(users_root)
    except ValueError:
        raise ValueError(_USER_KEY_ERROR_MSG) from None
    return cfg.data_paths_for(candidate)


def _ensure_fresh(paths: "cfg.DataPaths") -> None:
    """개인용 mcp_server._ensure_db의 얇은 미러 — per-user paths 버전.

    캐시(db)가 없거나 낡았으면(스키마/개수 불일치) yaml에서 재생성한다.
    """
    if not paths.db_path.exists() or db.cache_is_stale(paths.learnings_yaml, paths.db_path):
        db.rebuild_from_yaml(paths=paths)


def _normalize_tags(tags: "list[str] | str | None") -> "list[str] | None":
    """개인용 mcp_server._normalize_tags 미러 — MCP 클라이언트가 tags를 JSON
    문자열로 보내는 경우까지 관용적으로 처리한다."""
    if tags is None or isinstance(tags, list):
        return tags
    stripped = tags.strip()
    if not stripped:
        return None
    try:
        import json

        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return parsed
    except (ValueError, TypeError):
        pass
    return [tags]


# ---------------------------------------------------------------------------
# 저장소 동기화 배선(namu-58 4차) — user_repo.ensure_ready/push를 실제로 부른다.
#
# 이 절 전체의 존재 이유: 이 파일이 지금까지 user_repo를 한 번도 호출하지
# 않았다(grep 실측 0건) — 사용자가 GitHub 로그인·저장소 연결을 마쳐도 그
# 저장소가 실제 기억 읽기·쓰기에 전혀 쓰이지 않았다.
#
# ## TTL(사용자 결정 1)
#
# 매 조회(namu_recall/namu_search)마다 fetch하면 조회 하나에도 항상 GitHub
# 왕복이 걸려 지연이 커지고, 여러 사용자가 동시에 조회하면 GitHub API
# 레이트리밋에도 쉽게 부딪힌다. 그래서 "마지막으로 최신화한 지 TTL(기본 60초,
# `NAMU_REPO_SYNC_TTL_SEC`로 조절)이 지났을 때만" `ensure_ready`를 부른다 —
# 단, 사본이 아직 없으면(`.git` 없음, 첫 접속) TTL과 무관하게 반드시 부른다
# (clone 자체가 필요하므로 건너뛸 방법이 없다).
#
# ## "마지막 최신화 시각"을 어디에 두는가
#
# `user_repo.user_dir(key)/.git/` 밑의 파일 하나(`_sync_marker_path`)에 mtime을
# 실어 기록한다. 이 자리를 고른 근거 둘(작업 지시 제약 ⓐⓑ):
#   ⓐ 사용자 GitHub 저장소로 커밋되지 않아야 한다 — `.git/` 디렉터리 자체는
#      git이 스스로 쓰는 저장소 메타데이터 영역이라 `git add -A`가 애초에
#      손을 대지 않는다(실측: `.git/` 밑에 임의 파일을 만들어도
#      `git status --porcelain`에 전혀 나타나지 않는다 — user_repo.py의
#      `ensure_ready`가 clone 직후 곧바로 origin remote를 지우는 것과 같은
#      "로컬 전용 상태" 취급이다). 그래서 `.gitignore`/`.git/info/exclude`
#      등록조차 필요 없이 "커밋되지 않는다"가 구조적으로 보장된다.
#   ⓑ 프로세스 재시작을 넘겨 살아남아야 한다 — 이 서버는 stateless HTTP이고
#      워커가 여럿일 수 있으므로 파이썬 전역 dict(프로세스 하나에 갇힘)는
#      부적합하다. 파일은 디스크에 남아 재시작·다른 워커에서도 같은 값을
#      본다. 동시성 정합성을 엄격히 보장하지는 않는다(두 워커가 거의 동시에
#      mtime을 읽고 쓰면 레이스가 날 수 있다) — 하지만 이 값은 "정확한 락"이
#      아니라 "너무 자주 GitHub에 왕복하지 않기 위한 느슨한 힌트"이므로, 레이스의
#      최악의 결과는 fetch가 의도보다 한 번 더 일어나는 것뿐이고 사본 손상이나
#      데이터 유실로는 이어지지 않는다.
# ---------------------------------------------------------------------------
_REPO_SYNC_TTL_ENV = "NAMU_REPO_SYNC_TTL_SEC"
_DEFAULT_REPO_SYNC_TTL_SEC = 60.0
_SYNC_MARKER_FILENAME = "namu-cloud-last-sync"


def _repo_sync_ttl_sec() -> float:
    """TTL(초)을 환경변수에서 매 호출 시 읽는다(store_root()와 동일한 지연
    평가 원칙 — 테스트가 monkeypatch.setenv로 격리할 수 있어야 한다). 미설정
    시 기본 60초."""
    raw = os.environ.get(_REPO_SYNC_TTL_ENV, "").strip()
    if not raw:
        return _DEFAULT_REPO_SYNC_TTL_SEC
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{_REPO_SYNC_TTL_ENV} 값이 숫자가 아닙니다: {raw!r}. "
            f"{_REPO_SYNC_TTL_ENV} must be a number (seconds)."
        ) from exc
    if value < 0:
        raise ValueError(
            f"{_REPO_SYNC_TTL_ENV}는 0 이상이어야 합니다. "
            f"{_REPO_SYNC_TTL_ENV} must be >= 0."
        )
    return value


def _sync_marker_path(user_key: str) -> Path:
    """`user_key`의 마지막 `ensure_ready` 성공 시각을 적어 둘 파일 경로.

    `user_repo.user_dir(key)/.git/` 밑에 두는 이유는 위 절 docstring 참고.
    `.git`가 아직 없는(사본 자체가 없는) 사용자에게는 이 경로가 존재할 수
    없다는 점이 `_needs_sync`가 "사본 없음"을 판별하는 근거이기도 하다.
    """
    return user_repo.user_dir(user_key) / ".git" / _SYNC_MARKER_FILENAME


def _needs_sync(user_key: str, ttl_sec: float) -> bool:
    """지금 `user_repo.ensure_ready`를 불러야 하는지 판정하는 순수 판정 함수.

    ① 사본 자체가 없으면(`.git` 없음) TTL과 무관하게 항상 True(사용자 결정 1의
    "단, ~" 조항) — clone이 필요한 상황을 TTL로 건너뛸 방법이 없다.
    ② 사본은 있지만 마커 파일이 없으면(과거 이 배선이 없던 시절에 만들어진
    사본이거나, 마커 기록 자체가 실패했던 경우) "한 번도 최신화 기록을 남기지
    않은 상태"로 보고 True — "모르면 넘어가기"가 아니라 "모르면 최신화"가 이
    서버의 안전한 기본값이다(낡은 캐시를 계속 믿는 쪽보다 한 번 더 fetch하는
    쪽의 비용이 훨씬 싸다).
    ③ 그 외에는 마지막 최신화로부터 지난 시간이 TTL 이상인지로 판정한다.
    """
    target = user_repo.user_dir(user_key)
    if not (target / ".git").is_dir():
        return True
    marker = _sync_marker_path(user_key)
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return True
    return age >= ttl_sec


def _mark_synced(user_key: str) -> None:
    """방금 `ensure_ready`가 성공했다는 사실을 마커 파일에 남긴다.

    `ensure_ready`가 예외를 던진 호출 경로에서는 이 함수를 부르지 않는다(호출부
    `_ensure_repo_synced` 참고) — 실패한 시도를 "최신화 성공"으로 기록하면 다음
    조회가 낡은 사본을 TTL이 지나도록 그대로 믿게 된다.

    `.git` 폴더를 이 함수가 대신 만들어주지 않는다(의도적) — 실제
    `user_repo.ensure_ready`는 성공적으로 반환할 때 항상 `.git`을 이미 만들어
    둔 상태다. 만약 `.git`이 없는데도 이 함수가 불렸다면(정상 경로에서는 나올
    수 없는 모순 상태, 혹은 결함 있는 대역) `mkdir(parents=True)`로 `.git`을
    조용히 만들어버리면 `_needs_sync`가 "사본이 이미 있다"고 오판하게 되고,
    그러면 실제로는 사본이 없는데도 다음 TTL 동안 재시도(clone)를 건너뛰는
    쪽으로 안전성이 거꾸로 뒤집힌다 — 그래서 이 경우엔 그냥 마커 기록을
    건너뛴다(다음 호출이 다시 `_needs_sync`에서 True를 받아 재시도하는 것이
    "마커를 못 써서 한 번 더 부르는" 쪽이 훨씬 안전하다).
    """
    marker = _sync_marker_path(user_key)
    if not marker.parent.is_dir():
        logger.warning(
            "사용자(%s) 최신화 마커를 쓸 폴더(.git)가 없습니다 — ensure_ready가 "
            "성공했다고 보고했는데 사본 폴더가 실제로는 없는 모순 상태입니다. "
            "마커 기록을 건너뜁니다(다음 호출이 다시 최신화를 시도합니다).",
            user_key,
        )
        return
    marker.write_text(str(time.time()), encoding="utf-8")


def _ensure_repo_synced(conn: sqlite3.Connection, user_key: str) -> None:
    """TTL 판정 결과에 따라 필요할 때만 `user_repo.ensure_ready`를 부른다.

    `user_repo.RepoNotConnected`/`QuotaExceeded`/`GitCommandFailed`/
    `SizeCheckFailed`는 그대로(변환하지 않고) 밖으로 내보낸다 — 그중
    `RepoNotConnected`만 호출부(`_sync_or_reject`)가 사용자 안내 메시지
    (`ValueError`)로 바꿔치기한다. 나머지(용량 초과·git 실패·크기 조회 실패)는
    "사용자 입력이 틀렸다"가 아니라 "운영 중 실패"이므로 user_repo가 이미
    구성한(토큰 마스킹을 거친) 메시지를 그대로 내보내는 편이 맞다.
    """
    if not _needs_sync(user_key, _repo_sync_ttl_sec()):
        return
    user_repo.ensure_ready(conn, user_key)
    _mark_synced(user_key)


def _sync_or_reject(conn: sqlite3.Connection, user_key: str) -> None:
    """3도구(namu_recall/namu_search/namu_record) 공용 진입점(사용자 결정 2).

    저장소를 연결하지 않은 사용자는 `user_repo.RepoNotConnected`를 그대로
    내보내지 않고 `ValueError`로 감싼다 — 이 서버의 기존 사용자 입력 거부
    관례(`_validate_user_key`/`_resolve_via`)가 전부 ValueError이므로 타입을
    맞춘다. 메시지 내용 자체는 새로 짓지 않고 `RepoNotConnected`의 원문을
    그대로 재사용한다 — 그 메시지가 이미 "로그인하고 저장소를 연결하라"는
    온보딩 안내를 한국어+영어로 담고 있다(`user_repo._require_connected` 참고,
    사용자 결정 2가 명시적으로 재사용을 권한 지점이다).
    """
    try:
        _ensure_repo_synced(conn, user_key)
    except user_repo.RepoNotConnected as exc:
        raise ValueError(str(exc)) from exc


def _push_and_collect_warning(conn: sqlite3.Connection, user_key: str) -> "str | None":
    """namu_record 전용 — 로컬 기록이 끝난 뒤 push하되, 실패해도 기록 자체는
    성공으로 둔다(사용자 결정 3).

    왜 실패를 예외로 올리지 않는가: 이 함수가 불릴 때는 이미 `db.record`/
    `profile.record_fact`가 끝나 기억이 로컬 사본에 안전히 남아 있다. 그 뒤
    push가 `PushRejected`/`QuotaExceeded`/`GitCommandFailed` 등으로 실패해도
    다음 record 때 그 변경까지 함께 push된다(`user_repo.push`는 `git add -A`라
    이전에 못 올린 변경이 자동으로 함께 실린다). 반대로 여기서 예외를 던져
    도구 호출 자체를 실패시키면, 호출한 AI가 "저장 실패"로 판단해 같은 내용을
    중복 기록할 위험이 생긴다 — 로컬에는 이미 있는데 또 쓰는 셈이다.

    조용히 삼키지는 않는다 — `logger.warning`으로 반드시 남기고(운영자가 push
    실패 누적을 추적할 수 있어야 한다), 호출자에게도 알려야 하므로(사용자 결정
    3) 경고 문자열을 반환한다. None이면 push가 필요 없었거나(변경 없음)
    성공했다는 뜻이다.
    """
    try:
        user_repo.push(conn, user_key)
    except user_repo.UserRepoError as exc:
        logger.warning(
            "사용자(%s) 기록 이후 GitHub push 실패 — 기억은 로컬에 안전히 남아 "
            "있고 다음 기록 때 함께 재시도됩니다: %s",
            user_key, exc,
        )
        return (
            "기억은 저장했지만 방금 GitHub 저장소로 올리지 못했습니다(다음 기록 "
            f"때 함께 재시도됩니다): {exc} | Saved locally, but the sync to your "
            "GitHub repo failed just now (will retry automatically on the next "
            "record call)."
        )
    return None


# ---------------------------------------------------------------------------
# 3도구 — 이름·파라미터는 개인용 mcp_server.py와 동일(claude.ai 커넥터 호환).
# ---------------------------------------------------------------------------
@mcp.tool()
def namu_recall(
    query: str | None = None,
    task_type: str | None = None,
    limit: int = 5,
    project: str | None = None,
    ctx: Context | None = None,
):
    """Load relevant past memory for the requesting user (multi-tenant routing).

    Routes to the caller's own data directory via the `user` URL query param
    (append `?user=<your-key>` to the MCP URL), scoped strictly to this user's
    own memory (STORE_ROOT/users/<key>/).

    ALSO call this whenever the user asks "what's left to do" / "let's
    continue" / "where were we" — there is no session hook on the web, so the
    "tasks" field IS the briefing: every currently-open task with its exact
    re-entry point.

    Args:
      query: topic keywords (optional; omit to get the most recent learnings)
      task_type: filter by code/doc/analysis/other (optional; learnings only)
      limit: max learnings entries (default 5)
      project: which project's open tasks to include (folder name, e.g.
        'namu-agent'). Omit for all projects merged.
    Returns: {"memo": [...every sticky note currently up, oldest first...],
      "profile": [...active facts...], "learnings": [...lesson/note dicts...],
      "tasks": [...every OPEN task, bookmarked first then most-recent-activity
      first: {"project", "slug", "title", "last_ts", "next", "why",
      "pin_machine", "pin_ts"}, where `next` is the full re-entry point...]}.
    Raises: ValueError if this user has not logged in and connected a GitHub
      repository yet (onboarding incomplete) — the message explains where to
      go, in Korean and English.
    """
    key = _resolve_user(ctx)
    _resolve_via(ctx)  # ?client= 출처 태그 검증 (개인용 미러 — 없거나 형식 틀리면 거부)
    with closing(identity.connect()) as conn:
        _sync_or_reject(conn, key)  # TTL 기반 최신화 + 미연결 사용자 거부(사용자 결정 1·2)
    paths = _paths_for_user(key)
    _ensure_fresh(paths)
    with closing(sqlite3.connect(paths.db_path)) as conn:
        return {
            # memo가 맨 앞이다 — 웹에는 세션 훅이 없어 이 반환이 붙여둔 쪽지가
            # 다시 눈에 띌 유일한 경로다(개인용 mcp_server.namu_recall과 동일).
            "memo": memo.load_all(paths),
            "profile": profile.active(paths=paths),
            "learnings": db.recall(conn, query, task_type, limit),
            "tasks": _open_tasks_for_user(key, project),
        }


@mcp.tool()
def namu_search(
    query: str | None = None,
    bowl: str = "learnings",
    project: str | None = None,
    task: str | None = None,
    machine: str | None = None,
    via: str | None = None,
    since: str | None = None,
    until: str | None = None,
    outcome_filter: str | None = None,
    limit: int = 10,
    ctx: Context | None = None,
):
    """Search this user's accumulated memory (exact match, no recency
    fallback). Routed via the `user` URL query param, same as
    namu_recall/namu_record.

    Two bowls are searchable here:
      - 'learnings' (default): past lessons/notes.
      - 'tasks': the scrollback of task log lines across every task — use this
        for "what did I do yesterday", "what happened on this task", etc.
        `project` narrows it to one project folder; omit for all merged.

    `query` is optional — omit it to filter by axes alone (e.g. bowl='tasks',
    machine='hp', since='2026-07-24').

    Args:
      query: search terms (optional)
      bowl: 'learnings' (default) | 'tasks'
      project: project folder name (tasks only; omit for all merged)
      task: task name, substring match (tasks only)
      machine: which computer wrote the line, exact match (tasks only)
      via: which AI wrote it, exact match (tasks only)
      since/until: date or datetime bounds, inclusive (tasks only)
      outcome_filter: 'success'/'failure'/'partial' (learnings only)
      limit: max returned rows (default 10)
    Returns: learnings → {"results": [...], "summary": {...}}; tasks →
      {"bowl": "tasks", "results": [...{"ts","project","task_slug","tag",
      "machine","via","text","detail"}...], "count": N}
    Raises: ValueError if this user has not logged in and connected a GitHub
      repository yet (onboarding incomplete) — the message explains where to
      go, in Korean and English.
    """
    key = _resolve_user(ctx)
    _resolve_via(ctx)  # ?client= 출처 태그 검증 (개인용 미러 — 없거나 형식 틀리면 거부)
    if bowl not in ("learnings", "tasks"):
        raise ValueError(
            f"bowl은 'learnings' 또는 'tasks'여야 합니다: {bowl!r} — 개인 사실"
            "(profile)과 쪽지(memo)는 namu_recall이 통째로 돌려줍니다"
        )
    if bowl != "tasks" and project is not None:
        # 조용히 무시하지 않는다 — 무시하면 부른 쪽이 걸러진 줄 알고 잘못된 결론을
        # 낸다(개인용 db.search_bowl과 같은 태도).
        raise ValueError(f"project는 tasks 전용 축입니다 (bowl={bowl!r}에는 쓸 수 없습니다)")

    with closing(identity.connect()) as conn:
        _sync_or_reject(conn, key)  # TTL 기반 최신화 + 미연결 사용자 거부(사용자 결정 1·2)

    if bowl == "tasks":
        entries = _task_journal_for_user(
            key, project=project, since=since, until=until,
            machine=machine, task=task, via=via,
        )
        if query:
            q = query.lower()
            # 거른 **뒤에** 자른다 — 먼저 자르면 걸러질 결과가 사라진다.
            entries = [
                e for e in entries
                if q in (e["text"] or "").lower()
                or q in (e["detail"] or "").lower()
                or q in (e["tag"] or "").lower()
                or q in e["task_slug"].lower()
            ]
        entries = entries[:limit]
        return {"bowl": "tasks", "results": entries, "count": len(entries)}

    paths = _paths_for_user(key)
    _ensure_fresh(paths)
    with closing(sqlite3.connect(paths.db_path)) as conn:
        return db.search(
            conn, query, outcome_filter, limit,
            machine=machine, via=via, task=task, since=since, until=until,
        )


# ---------------------------------------------------------------------------
# 작업일지(tasks) — 회원 저장소 사본 안의 `tasks/<프로젝트>/<작업>/`
# ---------------------------------------------------------------------------
# namu-68은 이 그릇을 거절했다. 근거로 적혀 있던 것은 "코어의 tasks 저장 위치가
# `Path.home()/.namu/tasks`라서 요청별로 갈아끼울 자리가 없다"였는데, 그건 코어의
# **한 함수**(`task_resolve.tasks_root_for`)에만 해당하는 사실이었다. 이 서버는 그
# 함수를 쓸 이유가 없다 — 회원 폴더에서 뿌리를 직접 만들면 되고, 열람 화면
# (`web_auth._task_project_dirs`)이 이미 그렇게 읽고 있었다.
#
# 나머지 코어 함수는 전부 **폴더를 인자로 받으므로** 그대로 재사용한다
# (find_open_tasks·task_title·next_note·pins_by_slug·_latest_log_ts·_parse_log_line
# ·_display_text). 줄 형식·시각 해석·닫힘 판정을 여기에 베껴 오면 같은 log.md를
# 개인용과 클라우드가 서로 다르게 읽게 된다.
#
# 반대로 **쓰는 쪽**(줄 조립·task.md 서식·검증)은 개인용 mcp_server.py 안에만 있어
# 재사용할 수 없다(그 파일은 불러오는 순간 컨테이너 홈에 개인용 데이터를 만든다 —
# 56·57행이 모듈 수준에서 실행된다). 이 파일이 3도구를 통째로 미러한 것과 같은
# 이유·같은 방식으로 여기서도 미러한다.

# 프로젝트 이름은 회원이 보내는 값이 곧 폴더 이름이 된다 — `..`·`/` 같은 경로
# 문자를 허용하면 남의 서랍으로 걸어 나갈 수 있다. `_validate_user_key`와 같은
# 태도로 처음부터 좁게 막는다(개인용은 cwd에서 뽑으므로 이 위험이 없다).
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_PROJECT_NAME_ERROR = (
    "project(프로젝트 이름) 형식이 올바르지 않습니다 — 영숫자로 시작하고 "
    "영숫자·점·하이픈·밑줄만 쓸 수 있습니다(경로 문자 금지). 예: 'namu-agent'"
    "  |  Invalid 'project': letters/digits/dot/dash/underscore only."
)


def _validate_project_name(project: str) -> str:
    name = (project or "").strip()
    if not _PROJECT_NAME_RE.match(name) or ".." in name:
        raise ValueError(_PROJECT_NAME_ERROR)
    return name


def _tasks_root_for(user_key: str, project: str) -> Path:
    """그 회원의 `tasks/<프로젝트>` 폴더 (개인용 `tasks_root_for`의 회원별 판)."""
    return user_repo.user_dir(user_key) / "tasks" / _validate_project_name(project)


def _open_tasks_for_user(user_key: str, project: "str | None") -> list:
    """열린 작업 브리핑 — 개인용 `task_resolve.open_tasks_briefing()`과 같은 모양·
    같은 순서(책갈피 먼저, 그다음 최근 활동순)로 돌려준다.

    계산 자체는 `web_auth._open_task_rows`가 이미 하고 있다(열람 화면이 쓰는 것과
    글자 하나까지 같은 목록이어야 한다 — 화면과 도구가 다른 목록을 보여주면 어느
    쪽이 맞는지 사용자가 알 수 없다). 비공개 이름을 부르는 것은 web_auth가 코어의
    `_latest_log_ts`를 부르는 것과 같은 관례이고, 이름이 사라지면 시험이 먼저
    실패한다.
    """
    rows = web_auth._open_task_rows(user_key)
    if project is not None:
        name = _validate_project_name(project)
        rows = [r for r in rows if r["project"] == name]
    return rows


def _task_journal_for_user(
    user_key: str,
    project: "str | None" = None,
    since: "str | None" = None,
    until: "str | None" = None,
    machine: "str | None" = None,
    task: "str | None" = None,
    via: "str | None" = None,
) -> list:
    """작업일지 통합 조회 — 개인용 `task_resolve.journal()`의 회원별 판.

    코어 함수를 그대로 못 쓰는 곳은 **조회 대상 폴더를 정하는 첫 세 줄뿐**이다
    (그 함수는 컨테이너 홈을 훑는다). 줄을 해석하고 축으로 거르는 부분은 코어의
    `_parse_log_line`/`_display_text`/`_task_matches`/`_normalize_bound`를 그대로
    부른다 — 같은 파일을 개인용과 다르게 읽으면 안 된다.
    """
    if project is None:
        targets = [(d.name, d) for d in _task_project_dirs_for_user(user_key)]
    else:
        root = _tasks_root_for(user_key, project)
        targets = [(root.name, root)]

    since_bound = task_resolve._normalize_bound(since, end=False) if since else None
    until_bound = task_resolve._normalize_bound(until, end=True) if until else None

    entries: list = []
    for project_name, tasks_root in targets:
        try:
            log_files = list(tasks_root.glob("*/log.md"))
        except OSError:
            continue

        for log_path in log_files:
            task_slug = log_path.parent.name
            if task is not None and not task_resolve._task_matches(task_slug, task):
                continue
            try:
                lines = log_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue

            for index, line in enumerate(lines):
                parsed = task_resolve._parse_log_line(line)
                if parsed is None:
                    continue
                ts = parsed["ts"]
                if since_bound is not None and ts < since_bound:
                    continue
                if until_bound is not None and ts > until_bound:
                    continue
                if machine is not None and parsed["machine"] != machine:
                    continue
                if via is not None and parsed["via"] != via:
                    continue
                shown, rest = task_resolve._display_text(lines, index, parsed["text"])
                entries.append({
                    "ts": ts,
                    "project": project_name,
                    "task_slug": task_slug,
                    "tag": parsed["tag"],
                    "machine": parsed["machine"],
                    "via": parsed["via"],
                    "text": shown,
                    "detail": rest,
                })

    entries.sort(key=lambda e: (e["ts"], e["project"], e["task_slug"]), reverse=True)
    return entries


def _task_project_dirs_for_user(user_key: str) -> list:
    """그 회원 사본의 `tasks/<프로젝트>` 폴더 목록. 한 번도 기록이 없으면 빈 목록."""
    tasks_root = user_repo.user_dir(user_key) / "tasks"
    try:
        return sorted(d for d in tasks_root.iterdir() if d.is_dir())
    except OSError:
        return []


# ── 쓰기 (개인용 mcp_server.py 미러) ──────────────────────────────────────
# 작업을 닫는 태그는 이 둘뿐이다(코어 `_log_says_closed`가 보는 것도 이 둘).
_CLOSING_TAGS = ("완료", "중단")

# "닫는다"는 뜻으로 흔히 쓰이지만 실제로는 닫히지 **않는** 말들(namu-66).
_CLOSING_SYNONYMS = (
    "종료", "마무리", "끝", "종결", "완결", "닫음", "닫기", "done", "close", "closed", "finish",
)

_LOG_INDENT = "    "
_LOG_REASON_LABEL = "왜: "
_LOG_BODY_LABEL = "상세: "

_NEW_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _validate_task_tag_text(tag: "str | None", text: "str | None") -> tuple:
    """tag/text 정리 — 개인용 `mcp_server._validate_task_tag_text` 미러.

    닫는 뜻의 유의어를 거절하는 이유가 핵심이다: 태그는 자유 문자열이라 '종료'라고
    적어도 저장은 되지만 판정은 '완료'/'중단'만 보므로, 적은 쪽은 닫았다고 믿고
    목록에는 계속 열려 있게 된다.
    """
    tag = "기록" if tag is None else tag.strip()
    text = (text or "").strip()
    if not tag:
        raise ValueError("tag는 빈 값일 수 없습니다")
    if "]" in tag or "\n" in tag or "\r" in tag:
        raise ValueError("tag에는 ']'나 개행을 쓸 수 없습니다")
    if tag.lower() in _CLOSING_SYNONYMS:
        raise ValueError(
            f"tag={tag!r}는 작업을 닫지 못합니다 — 닫는 말은 '완료'(다 끝냄)와 "
            "'중단'(더 안 함) 둘뿐입니다. 정말 닫는 것이면 그 둘 중 하나로 다시 "
            "적고, 한 단계만 끝난 것이면 '기록'처럼 닫지 않는 말을 쓰세요"
        )
    if not text:
        raise ValueError("text는 필수입니다(빈 값 불가)")
    return tag, " ".join(text.split())


def _validate_new_task_slug(task: "str | None") -> str:
    """새 작업 이름 검증 — 폴더명으로 안전한 문자만 허용해 경로 조작을 차단한다."""
    task = (task or "").strip()
    if not task:
        raise ValueError("topic(작업 이름)은 필수입니다")
    if not _NEW_SLUG_RE.match(task):
        raise ValueError(
            f"작업 이름 {task!r}가 올바르지 않습니다 — 영문/숫자/하이픈(-)/"
            "언더스코어(_)만 허용되고 첫 글자는 영문/숫자여야 합니다"
        )
    return task


def _resolve_task_slug(tasks_root: Path, project: str, task: "str | None") -> str:
    """작업 이름을 폴더 이름으로 정규화한다(완전일치 우선, 없으면 앞부분 일치).

    없는 이름으로 폴더를 새로 만들지 않는다 — task.md(목적) 없이 log만 생기면
    나중에 아무도 못 읽는 유령 작업이 된다.
    """
    if not task or not task.strip():
        raise ValueError("topic(작업 이름)은 필수입니다")
    task = task.strip()

    try:
        candidates = sorted(d.name for d in tasks_root.iterdir() if d.is_dir())
    except OSError:
        candidates = []

    if task in candidates:
        return task

    prefix = [c for c in candidates if c.startswith(f"{task}-")]
    if len(prefix) == 1:
        return prefix[0]

    open_slugs = [d.name for d in task_resolve.find_open_tasks(tasks_root)]
    hint = f" (열린 작업: {', '.join(open_slugs)})" if open_slugs else " (열린 작업 없음)"
    if not prefix:
        raise ValueError(
            f"프로젝트 {project!r}에서 작업 {task!r}를 찾을 수 없습니다{hint}"
            " — 새로 만들려면 create=True와 reason(목적)을 함께 주세요"
        )
    raise ValueError(
        f"작업 {task!r}가 여러 후보와 일치합니다: {', '.join(prefix)}{hint} — "
        "더 구체적으로 지정하세요"
    )


def _append_task_log_line(task_dir: Path, line: str) -> None:
    """log.md에 한 줄 append. 파일이 개행으로 끝나지 않으면 먼저 개행을 넣는다."""
    log_path = task_dir / "log.md"
    needs_leading_nl = False
    if log_path.exists():
        with log_path.open("rb") as f:
            f.seek(0, 2)
            if f.tell() > 0:
                f.seek(-1, 2)
                needs_leading_nl = f.read(1) != b"\n"
    with log_path.open("a", encoding="utf-8") as f:
        if needs_leading_nl:
            f.write("\n")
        f.write(line + "\n")


def _log_block(head: str, reason: "str | None", body: "str | None") -> str:
    """머리줄 + (왜/상세) 이어지는 줄을 한 덩어리로 만든다(namu-65 3층).

    '생략' 한 단어는 줄 자체를 만들지 않는다 — 화면에서 감추기로 한 값을 파일에
    남기면 브리핑이 '왜: 생략'이라는 빈 소리를 하게 된다.
    """
    lines = [head]
    for label, value in ((_LOG_REASON_LABEL, reason), (_LOG_BODY_LABEL, body)):
        value = " ".join((value or "").split())
        if not value or cfg.is_omitted(value):
            continue
        lines.append(f"{_LOG_INDENT}{label}{value}")
    return "\n".join(lines)


def _unmet_done_when_warning(task_dir: Path, tag: str) -> str:
    """닫는 줄인데 task.md에 안 채운 완료조건이 남아 있으면 붙일 경고(namu-66).

    막지 않고 경고만 하는 이유: 이관·범위 축소로 닫는 것은 정당하고 실제로 자주
    있다. 다만 "무엇을 안 하고 닫는지"가 기록에 드러나야 한다.
    """
    if tag not in _CLOSING_TAGS:
        return ""
    try:
        lines = (task_dir / "task.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    unmet = [ln.strip()[5:].strip() for ln in lines if ln.strip().startswith("- [ ]")]
    unmet = [u for u in unmet if u and u != "..."]
    if not unmet:
        return ""
    listed = "\n".join(f"  · {u}" for u in unmet[:5])
    more = f"\n  · … 외 {len(unmet) - 5}개" if len(unmet) > 5 else ""
    return (
        f"\n⚠ 안 채운 완료조건 {len(unmet)}개가 남은 채로 [{tag}] 했습니다:\n"
        f"{listed}{more}\n"
        "  정말 충족했다면 task.md의 네모칸을 채우고, 안 하고 닫는 것이라면 "
        "왜 안 하는지를 이 줄에 남기세요(이관·범위 축소는 정당한 종결 사유입니다)."
    )


def _record_task_entry(
    user_key: str,
    project: "str | None",
    task: "str | None",
    text: "str | None",
    tag: "str | None",
    via: "str | None",
    reason: "str | None" = None,
    body: "str | None" = None,
) -> str:
    """작업일지 한 줄(또는 세 줄 묶음)을 append하고 적힌 그대로 돌려준다."""
    if project is None:
        raise ValueError(_TASKS_PROJECT_REQUIRED)
    tasks_root = _tasks_root_for(user_key, project)
    slug = _resolve_task_slug(tasks_root, project, task)
    tag, text = _validate_task_tag_text(tag, text)

    # 시각은 기준 시간대로 통일한 시계를 쓴다(cfg.now) — 컨테이너 호스트는 UTC라
    # 현지시각 그대로 적으면 같은 파일 안에서 PC가 남긴 줄과 선후가 뒤집힌다.
    ts = cfg.now().strftime("%Y-%m-%d %H:%M:%S")
    machine = cfg.NAMU_MACHINE
    line = f"[{tag}] {ts} {machine} · {text}"
    if via:
        line += f" (via {via})"
    block = _log_block(line, reason, body)

    task_dir = tasks_root / slug
    _append_task_log_line(task_dir, block)
    if tag in _CLOSING_TAGS:
        # 닫는 줄이면 이 기기 이름으로 꽂힌 책갈피도 같이 뺀다(namu-70).
        task_resolve.clear_pin_if_points_to(tasks_root, machine, slug)
    return block + _unmet_done_when_warning(task_dir, tag)


def _create_task_entry(
    user_key: str,
    project: "str | None",
    task: "str | None",
    title: "str | None",
    purpose: "str | None",
    done_when: "list[str] | None",
    text: "str | None",
    tag: "str | None",
    via: "str | None",
) -> str:
    """새 작업 폴더(task.md + log.md)를 만들고 `[시작]` 줄을 append한다."""
    if project is None:
        raise ValueError(_TASKS_PROJECT_REQUIRED)
    tasks_root = _tasks_root_for(user_key, project)
    slug = _validate_new_task_slug(task)

    purpose = (purpose or "").strip()
    if not purpose:
        raise ValueError(
            "새 작업에는 reason(목적)이 필수입니다 — 목적 없는 작업은 나중에 "
            "아무도 못 읽습니다"
        )

    # 폴더를 만들기 전에 검증한다 — 뒤에서 터지면 목적만 적힌 껍데기가 남는다.
    if (text or "").strip():
        follow_tag, follow_text = _validate_task_tag_text(tag, text)
    else:
        if (tag or "").strip():
            raise ValueError(
                f"tag={tag!r}만 주고 남길 내용이 비었습니다 — 둘을 함께 주세요"
            )
        follow_tag = follow_text = None

    task_dir = tasks_root / slug
    if task_dir.exists():
        raise ValueError(
            f"작업 {slug!r}는 프로젝트 {tasks_root.name!r}에 이미 있습니다 — 덮어쓸 "
            "수 없습니다(task.md는 불변 목적, log.md는 append-only). 기존 작업에 "
            "기록하려면 create 없이 부르세요"
        )

    # 호출자가 제목에 작업 이름을 이미 넣어 넘기는 일이 잦다 — 그대로 두면 머리줄이
    # `# <이름> — <이름> — 설명`이 되고 task.md는 불변이라 사후 수정도 못 한다.
    display_title = task_resolve.strip_slug_prefix((title or slug).strip() or slug, slug)
    if len(display_title) > task_resolve.TITLE_LINE_LIMIT:
        raise ValueError(
            f"제목이 너무 깁니다({len(display_title)}자 > "
            f"{task_resolve.TITLE_LINE_LIMIT}자) — 제목은 목록 한 줄에 실리는 "
            "'이름'입니다. 짧은 이름만 남기고 설명은 reason(목적)으로 옮기세요. "
            f"받은 제목: {display_title!r}"
        )

    machine = cfg.NAMU_MACHINE
    today = cfg.now().strftime("%Y-%m-%d")
    done_when_lines = (
        "\n".join(f"- [ ] {item}" for item in done_when) if done_when else "- [ ] ..."
    )

    task_md = (
        f"# {slug} — {display_title}\n"
        f"📅 생성 {today} [{machine}] · 🔗 관련: __\n"
        "\n"
        "## 목적\n"
        f"{purpose}\n"
        "\n"
        "## 완료조건\n"
        f"{done_when_lines}\n"
    )
    log_md = f"# log — {slug}\n(append만. 이 파일이 이 task의 권위 있는 기록이다)\n\n"

    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text(task_md, encoding="utf-8")
    (task_dir / "log.md").write_text(log_md, encoding="utf-8")

    ts = cfg.now().strftime("%Y-%m-%d %H:%M:%S")
    start_line = f"[시작] {ts} {machine} · 작업 생성, 목적·완료조건 확정"
    if via:
        start_line += f" (via {via})"
    _append_task_log_line(task_dir, start_line)

    summary = f"작업을 만들었습니다: {tasks_root.name}/{slug}\n{start_line}"

    if follow_text:
        follow_line = f"[{follow_tag}] {ts} {machine} · {follow_text}"
        if via:
            follow_line += f" (via {via})"
        _append_task_log_line(task_dir, follow_line)
        summary += f"\n{follow_line}"
    else:
        summary += (
            "\n⚠ 이어갈 지점([다음] 줄)이 비어 있습니다 — 브리핑에 "
            '"다음: (기록 없음)"으로 뜨고, 다음 세션은 어디서 시작할지 모릅니다. '
            f"지금 바로 namu_record(bowl='tasks', project={tasks_root.name!r}, "
            f"topic={slug!r}, status='다음', summary='<다음에 시작할 지점>')을 한 번 "
            "더 부르세요(만들 때 함께 주면 한 번에 들어갑니다)."
        )

    return summary


# 웹에는 "지금 이 폴더"라는 것이 없다 — 개인용은 cwd에서 프로젝트를 뽑지만
# 여기서는 뽑을 자리가 없으므로 명시를 요구한다(개인용의 웹 경로와 같은 규칙).
_TASKS_PROJECT_REQUIRED = (
    "작업일지 기록에는 project(프로젝트 이름)를 명시해야 합니다 — 이 주소에는 "
    "'지금 이 폴더'라는 것이 없습니다. 예: project='namu-agent'  |  Recording to "
    "the 'tasks' bowl requires an explicit 'project' here (no cwd on the web)."
)

# 도구 설명문은 손으로 쓰지 않고 코어의 표(config.FIELDS)에서 만든 것을 그대로
# 붙인다(namu-65의 규칙 — 설명문을 두 곳에 적으면 갈라지고, 갈라진 설명을 읽은 AI가
# 잘못된 그릇에 담는 것이 그 작업의 발단이었다). 클라우드에만 해당하는 사실(라우팅
# 키·프로젝트 명시·반환 모양)만 앞뒤에 덧붙인다.
_RECORD_TOOL_DESCRIPTION = (
    record_input.tool_description()
    + "\n\n"
    "── 이 클라우드 주소에서만 다른 점 ──\n"
    "- 기록은 요청 URL의 `?user=<키>`가 가리키는 **회원님 전용 저장소**에 남는다.\n"
    "- 작업일지(tasks) 그릇은 `project`(프로젝트 이름)를 반드시 함께 줘야 한다 — "
    "이 주소에는 '지금 이 폴더'라는 것이 없다.\n"
    "- 반환은 보통 새 기록의 id(문자열) 하나이고, 작업일지는 실제로 적힌 줄이다. "
    "알릴 것이 있을 때만 {\"id\": …, \"notices\": [...], \"warning\": …} 형태의 "
    "dict가 되므로, `isinstance(result, dict)`로 두 모양을 가른다."
)


@mcp.tool(description=_RECORD_TOOL_DESCRIPTION)
def namu_record(
    # ── 새 이름 (namu-65 3층 스키마) — 개인용 mcp_server.namu_record와 같은 순서
    bowl: str | None = None,
    summary: str | None = None,
    reason: str | None = None,
    body: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    category: str | None = None,
    tags: "list[str] | None" = None,
    project: str | None = None,
    confidence: str | None = None,
    supersedes: str | None = None,
    create: bool = False,
    done_when: "list[str] | None" = None,
    # ── 옛 이름 (그대로 불러도 새 칸으로 옮겨 저장하고 어디로 옮겼는지 알린다)
    task: str | None = None,
    outcome: str | None = None,
    task_type: str | None = None,
    verified_by: str | None = None,
    kind: str | None = None,
    subject: str | None = None,
    statement: str | None = None,
    source: str | None = None,
    text: str | None = None,
    tag: str | None = None,
    title: str | None = None,
    purpose: str | None = None,
    ctx: Context | None = None,
):
    """Record one memory into this user's own bowl (append-only), routed via
    the `user` URL query param. Field-by-field docs live in the tool
    description, which is generated from the core's field table
    (`record_input.tool_description()`) — do not restate them here (namu-65:
    two hand-written copies drift, and a drifted description is what made an
    AI put memories into the wrong bowl in the first place).

    이 함수 주석에는 **동작 순서와 클라우드 고유 규칙**만 적는다:

    (1) `record_input.normalize`가 그릇을 확정하고, 옛 이름을 새 이름으로 옮기고,
        그 그릇이 받지 않는 칸/빈 필수 칸/정해진 값 밖의 값을 거절한다. 저장소
        동기화(clone/pull)보다 **먼저** 부른다 — 어차피 거절될 호출 때문에 사용자
        저장소를 내려받는 것은 낭비이고, 입력 검증은 부작용이 없다.
    (2) 그릇별 저장 계층으로 넘기되, 전역 경로 대신 **그 사용자 전용 paths**를
        넘긴다(교훈=db.record, 개인 사실=profile.record_fact, 쪽지=memo.add).
        작업일지(tasks)만 paths가 아니라 회원 폴더의 `tasks/<프로젝트>`에 직접
        쓴다(그 그릇은 파일이 아니라 폴더 구조라 DataPaths에 자리가 없다).
    (3) 로컬 기록이 끝난 뒤에만 push를 시도한다(namu-58 4차 결정 3).

    Returns: 평소에는 새 기록의 id(str) — 이 흔한 경로의 모양은 종전 그대로다
      (반환값을 그대로 다음 호출의 `supersedes=`에 넣는 호출자가 계속 동작한다).
      알릴 것이 생겼을 때만 dict가 된다:
        - `notices`: 옛 이름을 새 칸으로 옮겼다는 등의 안내(옮겨놓고 알리지 않으면
          그것도 조용한 유실이다). 개인용 서버는 이 안내를 id 문자열 뒤에 이어
          붙이지만, 여기서는 id를 오염시키지 않으려고 별도 칸으로 돌려준다.
        - `warning`: 로컬 기록은 성공했지만 GitHub push가 실패했을 때(다음 기록 때
          함께 재시도되므로 기억이 유실되지는 않는다).
      두 모양은 `isinstance(result, dict)`로 가른다.
    Raises: ValueError — 입력이 규칙에 어긋날 때(그릇 미지정, 작업일지에 project
      누락 등), 또는 아직 로그인·저장소 연결을 마치지 않은 사용자일 때(안내
      메시지가 한국어+영어로 어디로 가야 하는지 알려준다). push 실패는 raise하지
      않는다 — 위 `warning` 참고.
    """
    key = _resolve_user(ctx)
    via = _resolve_via(ctx)  # ?client= 출처 태그 (개인용 미러 — 기록에 함께 저장)

    parsed = record_input.normalize({
        "bowl": bowl, "summary": summary, "reason": reason, "body": body,
        "topic": topic, "status": status, "category": category, "tags": tags,
        "project": project, "confidence": confidence, "supersedes": supersedes,
        "create": create, "done_when": done_when,
        "task": task, "outcome": outcome, "task_type": task_type,
        "verified_by": verified_by, "kind": kind, "subject": subject,
        "statement": statement, "source": source, "text": text, "tag": tag,
        "title": title, "purpose": purpose,
    })

    v = parsed.values
    v_summary = v.get("summary")
    v_reason = v.get("reason")
    v_body = v.get("body")
    v_topic = v.get("topic")
    v_tags = _normalize_tags(v.get("tags"))

    with closing(identity.connect()) as conn:
        _sync_or_reject(conn, key)  # TTL 기반 최신화 + 미연결 사용자 거부(사용자 결정 1·2)
        paths = _paths_for_user(key)
        _ensure_fresh(paths)
        if parsed.bowl == "tasks":
            # 작업일지만 반환이 id가 아니라 **실제로 적힌 줄**이다(개인용과 같다) —
            # 부른 쪽이 무엇이 어떻게 적혔는지 그대로 확인할 수 있어야 한다.
            if v.get("create"):
                # 새 작업에서 body는 "다음 세션이 시작할 지점"이다(namu-62 ③).
                # '생략'이면 줄을 만들지 않고 기존 경고가 그대로 뜨게 둔다.
                start_point = None if cfg.is_omitted(v_body) else v_body
                entry_id = _create_task_entry(
                    key, v.get("project"), v_topic, v_summary, v_reason,
                    v.get("done_when"), start_point,
                    (v.get("status") or "다음") if start_point else None, via,
                )
            else:
                entry_id = _record_task_entry(
                    key, v.get("project"), v_topic, v_summary, v.get("status"), via,
                    reason=v_reason, body=v_body,
                )
            warning = _push_and_collect_warning(conn, key)
            if warning or parsed.notices:
                result: dict = {"id": entry_id}
                if parsed.notices:
                    result["notices"] = parsed.notices
                if warning:
                    result["warning"] = warning
                return result
            return entry_id

        if parsed.bowl == "learnings":
            # kind는 없앤 칸이다 — status(성패)가 있으면 교훈, 없으면 단순 기록으로
            # 본다(개인용 mcp_server와 같은 판정).
            entry_id = db.record(
                v_topic, v.get("status"), v_reason,
                v.get("category") or "other", v.get("confidence") or "ai", v_tags,
                kind="lesson" if v.get("status") else "note",
                via=via, paths=paths, summary=v_summary, body=v_body,
            )
        elif parsed.bowl == "profile":
            entry_id = profile.record_fact(
                v_topic, supersedes=v.get("supersedes"),
                verified_by=v.get("confidence") or "human", tags=v_tags, via=via,
                paths=paths, summary=v_summary, reason=v_reason, body=v_body,
            )
        else:  # memo
            entry_id = memo.add(
                tags=v_tags, via=via, paths=paths,
                summary=v_summary, reason=v_reason, body=v_body,
            )

        # 로컬 기록이 끝난 뒤에만 push를 시도한다(사용자 결정 3) — 위에서 raise된
        # 경로(입력 거절 등)는 여기 도달하지 않으므로 push 자체가 시도되지 않는다.
        warning = _push_and_collect_warning(conn, key)

    if warning or parsed.notices:
        result: dict = {"id": entry_id}
        if parsed.notices:
            result["notices"] = parsed.notices
        if warning:
            result["warning"] = warning
        return result
    return entry_id


# ---------------------------------------------------------------------------
# 인증/전송 보안 — vendor/namu-agent/namu-plugin/http_server.py의 validate_settings
# (62~77줄) / _send_json (80~89줄) / AuthMiddleware (92~130줄) /
# _LOCALHOST_ALLOWED_HOSTS·_LOCALHOST_ALLOWED_ORIGINS (196~197줄) /
# _build_transport_security (200~226줄)를 그대로 미러링(import 아님 — 사용자 결정).
# v0.1.3부터 path_secret(URL 경로 인증, claude.ai 웹 호환)도 유효 인증으로 인정하도록
# validate_settings가 개인용과 동일하게 token 또는 path_secret을 검사한다(미러링).
# ---------------------------------------------------------------------------
def validate_settings(s: dict) -> None:
    """기동 시 인증 구성 점검.

    namu-59부터 이 서버의 인증은 **환경변수 설정 여부와 무관하게 항상 켜져
    있다** — 모든 MCP 요청이 `_PerUserSecretDispatcher`를 반드시 통과해야
    하고, 장부에 있는 사용자별 열쇠 없이는 어떤 경로로도 도구에 닿을 수 없다
    (열쇠가 없으면 404, 장부를 못 열어도 404 — 닫히는 방향).

    그래서 예전의 "token도 path_secret도 없으면 기동 거부" 검사는 의미를
    잃었다. NAMU_HTTP_PATH_SECRET(전원 공용 열쇠)은 더 이상 읽지 않으며,
    설정돼 있어도 아무 효과가 없다. NAMU_HTTP_TOKEN은 남겨 둔다 — 사용자별
    열쇠 **위에 덧대는** 선택적 2차 방어선이다(설정하면 헤더까지 맞아야
    통과). 인자를 그대로 받는 이유는 호출부·테스트 계약 유지다.
    """
    return


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AuthMiddleware:
    """토큰 헤더 검증. 순수 ASGI 3-인자 callable.

    token이 비어 있으면(무인증 로컬 테스트 구성) 무조건 통과시킨다.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        api_key = headers.get(b"x-api-key", b"").decode("latin-1")
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        token_bytes = self.token.encode("utf-8")

        authorized = False
        if api_key and hmac.compare_digest(api_key.encode("utf-8"), token_bytes):
            authorized = True
        elif auth_header.startswith("Bearer "):
            candidate = auth_header[len("Bearer ") :]
            if hmac.compare_digest(candidate.encode("utf-8"), token_bytes):
                authorized = True

        if not authorized:
            client = scope.get("client")
            addr = f"{client[0]}:{client[1]}" if client else "unknown"
            # 조용한 실패 금지 — 단 헤더 값 자체(토큰 후보)는 로그에 남기지 않는다.
            logger.warning("NAMU 라우팅 HTTP 인증 실패 (client=%s)", addr)
            await _send_json(send, 401, {"error": "unauthorized"})
            return

        await self.app(scope, receive, send)


# FastMCP가 host in (127.0.0.1/localhost/::1)일 때 자동 적용하는 기본값. 터널 경유
# 요청 허용을 위해 NAMU_HTTP_ALLOWED_HOSTS를 넣더라도 로컬 curl 스모크가 계속
# 동작해야 하므로, 이 기본값을 "대체"가 아니라 사용자 항목에 "합쳐서" 쓴다.
_LOCALHOST_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOCALHOST_ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def _build_transport_security(allowed_hosts: list[str]) -> TransportSecuritySettings | None:
    """NAMU_HTTP_ALLOWED_HOSTS(터널 경유 421 Misdirected Request 수정)로부터
    TransportSecuritySettings를 만든다.

    - allowed_hosts == ["*"]: DNS rebinding 보호 자체를 끈다.
    - 그 외 비어있지 않은 값: 보호는 유지한 채 FastMCP localhost 기본 3종에
      사용자 항목을 더한다(대체 금지).
    - 빈 리스트(미설정): None을 반환해 FastMCP 자동 기본값을 그대로 둔다.
    """
    if not allowed_hosts:
        return None
    if allowed_hosts == ["*"]:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_LOCALHOST_ALLOWED_HOSTS + allowed_hosts,
        allowed_origins=_LOCALHOST_ALLOWED_ORIGINS,
    )


# MCP 앱이 실제로 마운트되는 고정 경로. 바깥에서 들어오는 주소는 항상
# `/mcp/<사용자별 열쇠>`이고, _PerUserSecretDispatcher가 열쇠를 떼어내 신원을
# 판정한 뒤 경로를 이 값으로 바꿔 넘긴다 — FastMCP는 마운트 경로가 고정이라
# "주소마다 다른 경로"를 직접 받을 수 없기 때문이다.
_MCP_MOUNT_PATH = "/mcp"


def resolve_streamable_path(settings: dict) -> str:
    """MCP 앱의 마운트 경로. 항상 고정값이다.

    namu-59 이전에는 NAMU_HTTP_PATH_SECRET(전원 공용 열쇠 하나)을 경로에 실어
    `/mcp/<공용열쇠>`를 반환했다. 그 방식은 **인증이 아니었다** — 열쇠를 아는
    사람이면 누구나 `?user=`에 남의 이름표를 적어 넣어 남의 서랍을 열 수
    있었다(요청자가 스스로 밝히는 값을 그대로 믿는 구조). 지금은 사용자별
    열쇠가 그 자리를 대신하며, 그 판정은 마운트 경로가 아니라
    `_PerUserSecretDispatcher`가 한다.

    settings는 쓰지 않지만 인자를 남겨 둔다 — 호출부(build_app)와 기존
    테스트의 계약을 그대로 유지하기 위해서다.
    """
    return _MCP_MOUNT_PATH


class _PerUserSecretDispatcher:
    """`/mcp/<사용자별 열쇠>` → 신원 판정 → 고정 경로로 넘기는 순수 ASGI 3-인자
    미들웨어 (namu-59).

    이 클래스가 이 서버의 **인증 경계**다. 하는 일은 셋이다.

    1) 경로에서 열쇠를 떼어내 장부에서 찾는다. 못 찾으면 404 — 열쇠 형식이
       틀렸든, 없는 열쇠든, 장부를 못 열든 **전부 똑같이 404**다. 응답을
       구분해 주면 "이 열쇠는 형식은 맞다" 같은 단서가 새어 나가 열거 공격을
       돕는다. 서버 쪽 사정(장부 오류)만 로그에 남긴다.
    2) 찾아낸 user_key를 쿼리에 **덮어쓴다**. 요청자가 `?user=`를 직접 실어
       보내도 여기서 통째로 걷어내고 다시 쓰므로, 남의 이름표를 적어 넣는
       수법이 통하지 않는다(namu-59가 없앤 결함이 정확히 이것이다).
       도구 핸들러(_resolve_user)는 예전 그대로 `?user=`를 읽으면 된다.
    3) 경로를 고정 마운트 경로로 바꿔 MCP 앱에 넘긴다.

    실패는 항상 닫히는 방향이다 — 열쇠를 확인하지 못하면 통과시키지 않는다.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _extract_secret(path: str) -> "str | None":
        """`/mcp/<열쇠>`에서 열쇠만. 그 모양이 아니면 None.

        조각이 둘 이상이거나(`/mcp/a/b`) 비어 있으면 받지 않는다 — 경로 이탈
        문자는 identity 쪽 형식 검사에서도 다시 걸리지만, 모양 자체를 여기서
        좁혀 두는 편이 안전하다.
        """
        prefix = _MCP_MOUNT_PATH + "/"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix) :].rstrip("/")
        if not rest or "/" in rest:
            return None
        return rest

    @staticmethod
    def _query_with_user(raw_query: bytes, user_key: str) -> bytes:
        """쿼리에서 기존 user 항목을 전부 걷어내고 판정된 값으로 다시 쓴다.

        `?user=A&user=B`처럼 여러 번 실어 보내는 수법까지 막으려면 첫 항목만
        고쳐선 안 되고 **전부** 지운 뒤 하나만 넣어야 한다.
        """
        from urllib.parse import parse_qsl, urlencode

        pairs = [
            (k, v)
            for k, v in parse_qsl(raw_query.decode("latin-1"), keep_blank_values=True)
            if k != "user"
        ]
        pairs.append(("user", user_key))
        return urlencode(pairs).encode("latin-1")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        secret = self._extract_secret(scope.get("path", ""))
        user_key = None
        if secret is not None:
            try:
                with closing(identity.connect()) as conn:
                    row = identity.get_by_mcp_secret(conn, secret)
                    if row is not None:
                        user_key = row["user_key"]
                        identity.touch(conn, user_key)
            except Exception:
                # 장부를 못 열면 통과시키지 않는다(닫히는 방향). 열쇠 값 자체는
                # 로그에 남기지 않는다 — 로그가 곧 비밀 유출 경로가 된다.
                logger.exception("MCP 접속 열쇠 조회 실패 — 요청을 거부합니다")

        if user_key is None:
            await _send_json(send, 404, {"error": "not found"})
            return

        scope = dict(scope)
        scope["path"] = _MCP_MOUNT_PATH
        scope["raw_path"] = _MCP_MOUNT_PATH.encode("latin-1")
        scope["query_string"] = self._query_with_user(
            scope.get("query_string") or b"", user_key
        )
        await self.app(scope, receive, send)


# 로그인 없이 열리는 경로. 메뉴(ui.MENU)와 같은 목록이라 한쪽만 늘어날 수
# 없다 — 메뉴에 없는 공개 경로도, 문이 안 열린 메뉴도 생기지 않는다.
_PUBLIC_PATHS = frozenset(ui.PUBLIC_PATHS)


class _AuthOrMcpDispatcher:
    """`/auth/`(웹 로그인) vs 그 외 전부(MCP+Auth)를 가르는 순수 ASGI 3-인자
    디스패처(AuthMiddleware와 같은 관례).

    Starlette `Mount`로 감싸지 않는 이유: FastMCP의 streamable HTTP 앱은
    lifespan 이벤트에서 세션 매니저(StreamableHTTPSessionManager)를 기동한다.
    Mount 경유는 하위 앱까지 lifespan을 전달하는 라우팅 규칙에 기대야 해서
    실수하기 쉽다 — 여기서는 scope 자체를 직접 봐서 실수의 여지를 없앤다.
    lifespan scope에 대한 별도 분기가 없어도 안전한 이유: lifespan scope에는
    "path" 키가 없어 아래 `path.startswith("/auth/")` 검사가 항상 거짓이 되고,
    그 결과 자연히 mcp_app(=AuthMiddleware) 쪽으로 넘어간다. AuthMiddleware는
    http가 아닌 scope를 무조건 통과시키므로(92~130줄 참고), lifespan은 결국
    FastMCP의 streamable_http_app()에만 전달된다 — "lifespan은 MCP 앱에만"
    요구사항이 이 else 분기 하나로 충족된다.

    보안 회귀 방지: 기본값(else 분기)이 "인증 있는 쪽"이다 — 어떤 경로 조작
    (`/auth/../mcp` 등)으로도 리터럴 문자열이 `/auth/`로 시작하지 않으면 곧장
    AuthMiddleware를 거치므로, 인증 없이 MCP에 닿을 방법이 없다(auth_app 쪽으로
    잘못 분류되는 경우도 마찬가지로 안전한 방향 — auth_app에는 애초에 MCP 라우트가
    없다).

    공개 페이지(namu-70, 홈·시작하기 등)만 예외로 웹 쪽에 보낸다. **접두어가
    아니라 정확히 일치하는 경로만** 목록으로 연다 — `startswith`로 열면
    `/faq/../mcp` 같은 조작에 문이 열릴 여지가 생기지만, `==`로 보면 목록에 적힌
    그 글자 그대로가 아닌 모든 요청은 종전대로 닫히는 방향(MCP+인증)으로 간다.
    목록은 `ui.PUBLIC_PATHS` 한 곳에서 온다(메뉴와 문이 어긋나지 않게).
    """

    def __init__(self, auth_app, mcp_app):
        self.auth_app = auth_app
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and (
            path.startswith("/auth/") or path in _PUBLIC_PATHS
        ):
            await self.auth_app(scope, receive, send)
            return
        await self.mcp_app(scope, receive, send)


# ---------------------------------------------------------------------------
# 기동 엔트리포인트 — stateless HTTP, 경로는 path_secret 유무에 따라 /mcp 또는
# /mcp/<secret>. `/auth/`로 시작하는 요청은 인증 우회(로그인 전이라 토큰이 없는
# 것이 정상)로 web_auth 앱에, 그 외 전부는 기존 MCP 앱 + AuthMiddleware로 간다.
# ---------------------------------------------------------------------------
def build_app():
    settings = cfg.http_settings()
    validate_settings(settings)
    mcp.settings.stateless_http = True
    mcp.settings.streamable_http_path = resolve_streamable_path(settings)
    ts = _build_transport_security(settings.get("allowed_hosts", []))
    if ts is not None:
        mcp.settings.transport_security = ts
    mcp_app = AuthMiddleware(mcp.streamable_http_app(), settings["token"])
    # 사용자별 열쇠 판정이 토큰 검사보다 **바깥**이다 — 열쇠가 없는 요청은
    # 토큰 로직에 닿기 전에 404로 끊는다.
    mcp_app = _PerUserSecretDispatcher(mcp_app)
    auth_app = web_auth.build_auth_app()
    return _AuthOrMcpDispatcher(auth_app, mcp_app)


def main() -> None:
    host = os.environ.get("NAMU_HTTP_HOST", "127.0.0.1").strip()
    port_raw = os.environ.get("NAMU_HTTP_PORT", "8770").strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError(f"NAMU_HTTP_PORT 값이 정수가 아닙니다: {port_raw!r}") from exc

    app = build_app()

    import uvicorn

    # 접속 기록에 개인 열쇠가 평문으로 남지 않게 한다(namu-67) — 아래
    # _PerUserSecretDispatcher가 scope를 복사해 고치는 탓에 uvicorn은 원래 경로를
    # 보고 로그를 찍는다. 자세한 사정은 access_log 모듈 문서 참고.
    uvicorn.run(app, host=host, port=port, log_config=access_log.build_log_config())


if __name__ == "__main__":
    main()
