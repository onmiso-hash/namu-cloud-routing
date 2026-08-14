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

그릇(bowl)은 다섯 개 다 받는다. 교훈(learnings)·개인 사실(profile)·쪽지(memo)·
첨부 기록(attachments)은
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
import base64
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
import attach_files  # noqa: E402
import attachments  # noqa: E402
import config as cfg  # noqa: E402
import db  # noqa: E402
import identity  # noqa: E402
import memo  # noqa: E402
import profile  # noqa: E402
import project_policy  # noqa: E402
import record_input  # noqa: E402
import task_move  # noqa: E402
import task_resolve  # noqa: E402
import ticket_web  # noqa: E402
import tickets  # noqa: E402
import ui  # noqa: E402
import user_repo  # noqa: E402
import web_auth  # noqa: E402
from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

# 이 서버가 내주는 도구. **소개문도 이 목록에서 만든다** — 목록과 소개문이 갈라지면
# 붙은 AI가 없는 도구를 부른다. 셀프호스팅 쪽이 실제로 그랬고(소개 7종/노출 3종),
# 클라우드는 반대로 소개문이 통째로 없어 붙은 AI가 그릇들이 무엇인지 안내받지
# 못했다(2026-08-05 두 서버를 띄워 실측). 코어(vendor/namu-agent)의 같은 함수를
# 쓰므로 두 경로의 소개문이 한 원본에서 나온다.
EXPOSED_TOOLS = frozenset({
    "namu_recall", "namu_search", "namu_record",
    "namu_upload_file", "namu_list_files", "namu_download_file",
    "namu_delete_file",
    "namu_create_upload_ticket", "namu_create_download_ticket",
    "namu_check_ticket",
    "namu_task_move",
})

mcp = FastMCP(
    "namu-cloud-routing",
    # upload_takes_path=False — 이 서버의 namu_upload_file에는 파일 경로 칸이 없다
    # (글자 원문 하나만 받는다). 소개문이 "디스크의 파일을 올린다"고 말하면 파일을
    # 든 웹 AI가 넣을 칸을 못 찾아 파일을 읽어 base64로 옮기기 시작한다 — 2026-08-07
    # 21:04에 실제로 그렇게 됐다. 상세는 record_input._WEB_TOOL_LINES 위 설명.
    instructions=record_input.server_instructions(
        EXPOSED_TOOLS, upload_takes_path=False,
    ),
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

    Every bowl is searchable here (same as a self-hosted NAMU server):
      - 'learnings' (default): past lessons/notes.
      - 'tasks': the scrollback of task log lines across every task — use this
        for "what did I do yesterday", "what happened on this task", etc.
        Each task's brief (its title, purpose and done-criteria) is in here
        too, as one whole entry tagged '설명서' — that is what answers
        "what was that task about?".
        `project` narrows it to one project folder; omit for all merged.
      - 'profile': facts about the user (superseded ones excluded).
      - 'memo': sticky notes currently up, oldest first.
      - 'attachments': the log of files uploaded to this user's repo — what
        was uploaded, why, its size and whether it is still there. Sizes are
        read from this log only, never asked of the repository.
        `project`/`task` narrow it.

    `query` is optional — omit it to filter by axes alone (e.g. bowl='tasks',
    machine='hp', since='2026-07-24').

    Args:
      query: search terms (optional)
      bowl: 'learnings' (default) | 'tasks' | 'profile' | 'memo' | 'attachments'
      project: project folder name (tasks/attachments only; omit for all merged)
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
    # 허용 목록을 손으로 적지 않는다 — 코어(cfg.BOWL_NAMES)를 그대로 본다.
    # 손으로 적어 두었던 탓에 코어가 그릇을 다 받게 된 뒤에도 이 서버만
    # 두 그릇에서 멈춰 있었다(2026-08-05 실측: 셀프호스팅은 profile·memo 검색
    # 성공, 클라우드는 거절). 셀프호스팅에서 되는 것이 여기서 안 되면 안 된다.
    if bowl not in cfg.BOWL_NAMES:
        raise ValueError(
            f"bowl은 {list(cfg.BOWL_NAMES)} 중 하나여야 합니다: {bowl!r}"
        )
    if bowl not in ("tasks", "attachments") and project is not None:
        # 조용히 무시하지 않는다 — 무시하면 부른 쪽이 걸러진 줄 알고 잘못된 결론을
        # 낸다(개인용 db.search_bowl과 같은 태도).
        raise ValueError(
            f"project는 tasks·attachments 전용 축입니다 (bowl={bowl!r}에는 쓸 수 없습니다)"
        )

    with closing(identity.connect()) as conn:
        _sync_or_reject(conn, key)  # TTL 기반 최신화 + 미연결 사용자 거부(사용자 결정 1·2)

    if bowl == "tasks":
        entries = _task_journal_for_user(
            key, project=project, since=since, until=until,
            machine=machine, task=task, via=via,
        )
        if query:
            # 거르는 규칙은 코어의 `matches_all_tokens`를 부른다 — 낱말별로 나눠
            # 모두 포함(AND), 순서 무관. **여기에 규칙을 베끼지 않는 것이 요점이다**:
            # 예전에는 "검색어를 통째로 부분일치"를 이 자리에 적어 두었고, 코어가
            # 낱말 AND로 개선됐을 때(fts5-memo-tasks-index) 다섯 그릇 중 작업일지
            # 하나만 옛 방식으로 남았다 — 나머지 넷은 코어를 부르므로 자동으로
            # 따라갔다. 폴더를 정하는 부분만 회원별로 다를 뿐, 무엇을 걸러낼지는
            # 개인용과 같아야 한다.
            # 거른 **뒤에** 자른다 — 먼저 자르면 걸러질 결과가 사라진다.
            entries = [
                e for e in entries
                if db.matches_all_tokens(
                    query, e["text"], e["detail"], e["tag"], e["task_slug"]
                )
            ]
        entries = entries[:limit]
        return {"bowl": "tasks", "results": entries, "count": len(entries)}

    paths = _paths_for_user(key)
    _ensure_fresh(paths)
    with closing(sqlite3.connect(paths.db_path)) as conn:
        if bowl == "learnings":
            # 옛 반환 형태(summary 포함)를 그대로 지킨다 — 붙어 있는 AI가 이 모양을
            # 보고 있다.
            return db.search(
                conn, query, outcome_filter, limit,
                machine=machine, via=via, task=task, since=since, until=until,
            )
        # 개인 사실·쪽지·첨부 기록 — 거르는 규칙을 여기 베끼지 않고 코어에 맡긴다.
        # 베끼면 그 순간 두 서버가 또 갈라진다. `paths`로 이 사용자 폴더를 가리킨다.
        # project·task 축은 첨부 기록에서만 실제로 걸린다 — 다른 그릇에 얹어 넘기면
        # 코어가 조용히 무시해, 부른 쪽은 걸러진 줄 알고 잘못된 결론을 낸다.
        extra = {"project": project, "task": task} if bowl == "attachments" else {}
        return db.search_bowl(
            conn, bowl=bowl, query=query,
            machine=machine, via=via, since=since, until=until,
            limit=limit, paths=paths, **extra,
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

    작업 설명서(task.md)도 같은 목록에 담는다(2026-08-08). 파싱은 코어의
    `parse_task_doc`을 부르며, 여기에 베끼지 않는 이유는 위와 같다 — 개인용이
    `task_docs()`로 하는 일을 폴더 규칙만 바꿔 하는 것이다.
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

        # 작업 설명서 — 축은 일지 줄과 똑같이 건다. 한 그릇에 섞기로 한 이상 축이
        # 종류마다 다르게 들으면 결과를 믿을 수 없기 때문이다.
        try:
            doc_files = list(tasks_root.glob("*/task.md"))
        except OSError:
            doc_files = []

        for doc_path in doc_files:
            task_slug = doc_path.parent.name
            if task is not None and not task_resolve._task_matches(task_slug, task):
                continue
            entry = task_resolve.parse_task_doc(doc_path, project_name)
            if entry is None:
                continue
            ts = entry["ts"]
            if since_bound is not None and ts < since_bound:
                continue
            if until_bound is not None and ts > until_bound:
                continue
            if machine is not None and entry["machine"] != machine:
                continue
            # 설명서에는 `(via ...)` 꼬리표가 없다 — via 축을 주면 항상 빠진다.
            if via is not None and entry["via"] != via:
                continue
            entries.append(entry)

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
    """새 작업 폴더(task.md + log.md)를 만들고 `[시작]` 줄을 append한다.

    **자리는 넘어온 이름이 아니라 규칙이 정한다** — 이미 있는 방 이름이나
    `web-project`를 주면 그대로 만들고, 그 밖의 경우에는 만들지 않고 **회원에게
    보여줄 방 목록**을 돌려준다(ValueError). 이 주소에서는 **새 프로젝트가 생기지
    않는다.** 판정과 문안은 코어 `project_policy`에 있고 개인용 mcp_server도 같은
    모듈을 쓴다.

    이 주소는 project를 매번 자유 텍스트로 받았고(cwd가 없어서다), 그 글자를 AI가
    회원에게 묻지 않고 지어낼 수 있었다 — 실사고: 회원이 "블로그 자동 요약 봇"
    아이디어를 기록해 달라고만 했는데 'blog-summary-bot'이라는 프로젝트가 확인
    없이 생겼다. 그 값을 검사하는 게이트를 두 판 만들었고 두 판 다 뚫렸다(AI가
    채우는 값을 보는 검사는 AI가 그 값을 채우면 열린다). 지금은 검사하지 않고,
    그 글자가 **새 폴더를 만들 수 없게** 했다 — 남은 쓰임은 이미 있는 방을 고르는
    것뿐이라 일지를 덧붙이는 일과 같은 무게가 된다.

    회원 격리는 그대로다 — `web-project`는 회원 폴더 **안**의 이름이라 회원마다
    제 방이고, 이름이 같아도 섞이지 않는다. 고를 수 있는 방 목록도 그 회원 것뿐이다.
    """
    resolved_project = project_policy.resolve_create_project(
        project,
        is_web=True,
        existing=sorted(d.name for d in _task_project_dirs_for_user(user_key)),
        person="회원",
    )
    tasks_root = _tasks_root_for(user_key, resolved_project)

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
    # ── 첨부 기록 전용 2칸 (namu-file-upload-download 4단계)
    path: str | None = None,
    bytes: int | None = None,
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
        넘긴다(교훈=db.record, 개인 사실=profile.record_fact, 쪽지=memo.add,
        첨부 기록=attachments.record_attachment).
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
        "path": path, "bytes": bytes,
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
        elif parsed.bowl == "memo":
            entry_id = memo.add(
                tags=v_tags, via=via, paths=paths,
                summary=v_summary, reason=v_reason, body=v_body,
            )
        else:  # attachments — 첨부 기록(namu-file-upload-download 4단계)
            # 이 자리가 예전에는 `else: # memo`였다. 그릇이 하나 늘 때 그대로 두면
            # 첨부 기록이 **말없이 쪽지로 저장된다** — 잘못 담겨도 아무도 모르는
            # 그 경로가 이 시스템이 없애려는 결함이라, 그릇마다 명시 분기로 둔다.
            entry_id = attachments.record_attachment(
                path=v.get("path"), bytes_=v.get("bytes"), status=v.get("status"),
                summary=v_summary, reason=v_reason, body=v_body,
                topic=v_topic, project=v.get("project"),
                supersedes=v.get("supersedes"), tags=v_tags, via=via, paths=paths,
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
# 작업 옮기기 (new-project-rule 설계서 4단계, `docs/new_project_rule.md` 7장)
#
# 판정 로직(코어 `task_move.py`)을 여기 옮겨 적지 않는다 — `_create_task_entry`가
# `project_policy`를 부르는 것과 정확히 같은 이유다. 1차 판이 판정을 이 파일에
# 그대로 베껴 적었다가 코어만 고쳐 배포해 웹이 그대로 뚫린 사고가 있었다(원인을
# 찾는 데 세션 절반이 들었다 — 경위는 이 파일 첫머리 배경 주석과 코어
# `project_policy.py` 문서 참고). 이 도구가 하는 일은 셋뿐이다:
#   ① 회원 격리 — 목적지 후보(`known`)를 그 회원의 방 목록으로만 좁힌다.
#   ② 클라우드 전용 값(project 필수, cfg.now()/cfg.NAMU_MACHINE, 회원 호칭)을
#      코어 함수에 인자로 넘긴다.
#   ③ 쓰기 전 최신화·쓰기 뒤 push는 이 파일의 다른 쓰기 도구와 같은 배선
#      (`_sync_or_reject`/`_push_and_collect_warning`)을 그대로 쓴다.
# ---------------------------------------------------------------------------
_TASK_MOVE_DESCRIPTION = (
    "Move a task's whole folder (task.md + log.md) from one room to another, "
    "inside this member's own storage only (multi-tenant mirror of the "
    "personal tool of the same name — the actual move/permission logic lives "
    "in vendor/namu-agent's `task_move` module, not in this server).\n"
    "- `to` must already be one of THIS MEMBER's own rooms. An unknown or "
    "missing name never creates one — the error message IS the numbered room "
    "list to show the member, and that list only ever contains their own "
    "rooms (another member's room names never appear in it, and can never be "
    "a destination, even if you already know the exact name).\n"
    "- `project` (the source room) is required — there is no 'current "
    "folder' on the web, same rule as recording to the tasks bowl.\n"
    "- Bookmarks (`.pin.<machine>`) travel with the task to the new room, "
    "unless the destination room already has that machine's bookmark on "
    "something else — then only the source bookmark is dropped (never "
    "silently overwriting what another machine already pinned there).\n"
    "- Closed tasks ([완료]/[중단]) can be moved too — tidying up finished "
    "work into the right room is a common, legitimate reason to move.\n"
    "- If a task with the same name already exists in the destination room, "
    "this is rejected and nothing moves (log.md is append-only; merging two "
    "of them would make it impossible to tell which lines came from where).\n"
    "- ⚠ Do not call this twice at once on the same task (e.g. from two open "
    "sessions). log.md is designed for union-merge across machines/sessions "
    "— each caller only ever appends its own lines. If one call is mid-move "
    "while another appends to the same task's log.md in the old room, that "
    "line either never reaches the new location or collides at the next "
    "sync. There is no cross-call lock; this tool pushes to the member's "
    "repo immediately after moving, to keep the danger window as short as "
    "possible."
)


@mcp.tool(description=_TASK_MOVE_DESCRIPTION)
def namu_task_move(
    task: str,
    to: str,
    project: "str | None" = None,
    ctx: Context | None = None,
) -> "str | dict":
    key = _resolve_user(ctx)
    if project is None:
        raise ValueError(_TASKS_PROJECT_REQUIRED)
    source_root = _tasks_root_for(key, project)

    with closing(identity.connect()) as conn:
        _sync_or_reject(conn, key)  # TTL 기반 최신화 + 미연결 사용자 거부

        slug = _resolve_task_slug(source_root, project, task)

        # 목적지 후보는 반드시 이 회원의 방만이다 — `_create_task_entry`가
        # `project_policy.resolve_create_project`에 넘기는 `existing`과 같은
        # 발상이다. 코어 `task_move.resolve_move_destination`은 이 목록 밖으로
        # 절대 고르지 못한다(없으면 ValueError로 이 목록 자체를 돌려준다).
        known = sorted(d.name for d in _task_project_dirs_for_user(key))
        dest_project = task_move.resolve_move_destination(to, known=known, person="회원")
        dest_root = _tasks_root_for(key, dest_project)

        ts = cfg.now().strftime("%Y-%m-%d %H:%M:%S")
        result = task_move.move_task(
            source_root=source_root, dest_root=dest_root, slug=slug,
            machine=cfg.NAMU_MACHINE, ts=ts,
        )

        # 옮긴 직후 곧바로 밀어 올린다 — 다른 곳이 옛 자리 log.md에 줄을 붙일 수
        # 있는 창을 좁히는 것이 이 도구가 낼 수 있는 유일한 조치다(잠금은 없다).
        # push 실패는 다른 쓰기 도구들과 같은 배선으로 raise하지 않고 경고로 담는다.
        warning = _push_and_collect_warning(conn, key)

    note = ""
    if result["moved_pins"]:
        machines = ", ".join(p["machine"] for p in result["moved_pins"])
        note += f" 책갈피({machines})도 함께 옮겼습니다."
    if result["dropped_pins"]:
        machines = ", ".join(p["machine"] for p in result["dropped_pins"])
        note += (
            f" {dest_project!r}에 이미 책갈피가 있던 기기({machines})는 원본 책갈피만 "
            "뗐습니다(덮어쓰지 않았습니다)."
        )

    summary = f"작업 {slug!r}를 {project!r}에서 {dest_project!r}로 옮겼습니다.{note}"
    if warning:
        return {"summary": summary, "warning": warning}
    return summary


# ---------------------------------------------------------------------------
# 첨부 파일 네 도구 (namu-file-upload-download 5·6단계)
#
# 파일은 회원 GitHub 저장소와 **직접** 오간다(attach_files 모듈) — 이 서버 사본을
# 거치지 않는다. 사본에 놓았다 지우면 그 삭제가 다음 push에 실려 GitHub에서도
# 파일이 사라지는 것이 실측으로 확인됐기 때문이다.
#
# 파일을 올리거나 지우면 GitHub에 이 서버가 모르는 커밋이 하나 생긴다. 그 상태로
# 첨부 기록을 push하면 **반드시 거부된다** — 사본이 한 커밋 뒤처져 있기 때문이다.
#
# 설계서 6절은 "최신화 표시만 지워 다음 호출이 맞추게 한다"였고 처음엔 그대로
# 구현했는데, 2026-08-07 첫 실사용에서 그게 틀린 것이 드러났다: 파일은 올라갔지만
# 올린 기록이 저장소에 안 들어가고, 붙은 AI가 "push가 거부됐다"는 경고를 회원에게
# 그대로 전했다. 미룬 최신화는 **이번 호출의 push를 반드시 실패시킨다** — 다음
# 호출이 아니라 지금 맞춰야 한다.
#
# 순서도 중요하다: 최신화(`reset --hard`)는 사본의 안 커밋된 변경을 지우므로,
# **기록을 쓰기 전에** 맞춰야 한다. 뒤집으면 방금 쓴 첨부 기록이 날아간다.
# ---------------------------------------------------------------------------
def _resync_after_repo_change(conn: sqlite3.Connection, user_key: str) -> None:
    """GitHub에 우리가 만든 커밋을 사본에 즉시 반영한다(첨부 올리기·지우기 직후).

    실패해도 도구를 실패로 돌리지 않는다 — 파일은 이미 GitHub에 올라갔고, 여기서
    예외를 던지면 회원이 같은 파일을 또 올린다. 대신 최신화 표시를 지워 다음
    호출이 반드시 다시 맞추게 하고, 경위를 로그에 남긴다.
    """
    try:
        user_repo.ensure_ready(conn, user_key)
        _mark_synced(user_key)
        return
    except Exception as exc:
        logger.warning(
            "사용자(%s) 첨부 변경 뒤 사본 최신화에 실패했습니다: %s — "
            "최신화 표시를 지워 다음 호출이 다시 맞추게 합니다.",
            user_key, exc,
        )
    try:
        _sync_marker_path(user_key).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("사용자(%s) 최신화 표시를 지우지 못했습니다: %s", user_key, exc)


def _record_attachment_entry(
    paths, *, path: str, size: int, status: str, summary: str, reason: str,
    body: str, topic, project, tags, via,
) -> str:
    """첨부 기록 한 건을 남긴다 — 올리기·지우기 도구가 공통으로 쓴다.

    기억 도구(namu_record)로 아무나 쓰게 두지 않고 도구가 직접 남기는 이유:
    실제 파일 없이 기록만 있는 유령 항목이 생기면 목록이 거짓말을 한다.
    """
    return attachments.record_attachment(
        path=path, bytes_=size, status=status,
        summary=summary, reason=reason, body=body,
        topic=topic, project=project, tags=_normalize_tags(tags), via=via,
        paths=paths,
    )


def normalize_meta(meta: dict) -> dict:
    """첨부 한 건에 딸린 설명 칸들을 검사하고 다듬는다.

    올리기 도구와 올리기 티켓이 **같은 칸을 같은 규칙으로** 받아야 하므로 판정을
    한 곳에 둔다. 티켓은 발급 시점에 이 함수를 한 번 통과하고, 파일이 도착했을 때
    `store_file`이 한 번 더 통과한다 — 발급과 도착 사이에 저장해 둔 값이 손대졌을
    가능성을 거르는 자리다.
    """
    meta = dict(meta or {})
    for field_name in ("summary", "reason"):
        if not (meta.get(field_name) or "").strip():
            raise ValueError(
                f"'{field_name}'은 첨부에도 필수입니다 — 파일 몸통은 각 PC로 "
                "내려오지 않으므로, 나중에 이 파일을 찾는 단서는 이 설명뿐입니다."
            )
        meta[field_name] = meta[field_name].strip()
    # body는 일부러 필수가 아니다(2026-08-07 첫 실사용). 다른 기억처럼 필수로 뒀더니
    # 붙은 AI가 그 칸을 빼고 불렀고, 거절당할 때마다 **파일 내용 전체를 처음부터 다시
    # 써서** 재시도했다 — 회원 눈에는 몇 분째 멈춘 것으로 보였다. 첨부에서는 파일
    # 자체가 원문이라 애초에 요구할 이유도 없었다.
    meta["body"] = (meta.get("body") or "").strip() or cfg.OMITTED
    meta["topic"] = meta.get("topic") or None
    meta["project"] = meta.get("project") or None
    meta["tags"] = _normalize_tags(meta.get("tags"))
    return meta


# ---------------------------------------------------------------------------
# store_file — 파일 한 개가 회원 저장소에 자리 잡기까지의 전 과정.
#
# 이 함수를 따로 뗀 이유(설계서 11절 1단계): 파일이 들어오는 문이 둘이 됐다.
# 하나는 MCP 도구(`namu_upload_file`)이고 다른 하나는 티켓 주소로 들어오는
# 웹/curl 전송(`POST /u/<티켓>`)이다. 두 문이 각자 이 순서를 베껴 쓰면 한쪽만
# 고쳐지는 날이 반드시 오는데, 그 순서에는 이미 실사용에서 한 번 데인 함정이
# 들어 있다 — 사본 최신화를 **기록보다 먼저** 해야 한다는 것(바로 위 절 참고).
# ---------------------------------------------------------------------------
def store_file(
    conn: sqlite3.Connection,
    user_key: str,
    name: str,
    content: bytes,
    meta: dict,
    via: str,
) -> dict:
    """파일 한 개를 회원 저장소에 올리고 첨부 기록까지 남긴다.

    돌려주는 것: `{"id", "path", "bytes", "status", "seconds"}`, 그리고 push가
    밀렸을 때만 `"warning"`. 회원 장부 커넥션(`conn`)은 호출부가 열고 닫는다 —
    웹 라우트와 MCP 도구가 각자 자기 요청 수명에 맞춰 여닫아야 하기 때문이다.
    """
    meta = normalize_meta(meta)

    # 단계별 시간을 재서 함께 돌려준다(2026-08-07). 회원이 "너무 느리다"고 했는데
    # 서버 안에서 무엇이 오래 걸렸는지 볼 방법이 없어 추측만 오갔다 — 붙은 AI가
    # 이 값을 화면에 보여주면 "AI가 파일을 글자로 옮기느라 느린 것"인지 "서버가
    # 느린 것"인지가 그 자리에서 갈린다. 로그가 아니라 반환값에 싣는 이유가
    # 그것이다(서버 로그는 홈서버에 들어가야 볼 수 있다).
    timings: dict = {}
    t0 = time.perf_counter()

    def _mark(label: str, since: float) -> float:
        now = time.perf_counter()
        timings[label] = round(now - since, 2)
        return now

    _sync_or_reject(conn, user_key)
    t = _mark("사본_최신화_전", t0)
    result = attach_files.upload(
        conn, user_key, name, content,
        attach_files.commit_message("올림", name),
    )
    t = _mark("깃허브_올리기", t)
    # 기록을 쓰기 **전에** 맞춘다 — reset --hard가 안 커밋된 변경을 지우므로
    # 순서를 뒤집으면 방금 쓴 첨부 기록이 날아간다.
    _resync_after_repo_change(conn, user_key)
    t = _mark("사본_최신화_후", t)

    paths = _paths_for_user(user_key)
    _ensure_fresh(paths)
    status = "새 판" if result["replaced"] else "올림"
    entry_id = _record_attachment_entry(
        paths, path=result["path"], size=result["bytes"], status=status,
        summary=meta["summary"], reason=meta["reason"], body=meta["body"],
        topic=meta["topic"], project=meta["project"], tags=meta["tags"], via=via,
    )
    t = _mark("기록_저장", t)
    warning = _push_and_collect_warning(conn, user_key)
    _mark("기록_올리기", t)
    timings["합계"] = round(time.perf_counter() - t0, 2)
    logger.info("첨부 올리기 시간(초): %s", timings)

    out = {
        "id": entry_id,
        "path": result["path"],
        "bytes": result["bytes"],
        "status": status,
        "seconds": timings,
    }
    if warning:
        out["warning"] = warning
    return out


# ---------------------------------------------------------------------------
# 왜 이 도구에 base64 칸이 없나 (2026-08-07, 사용자 지시)
#
# 처음에는 `content_base64`를 남겨 뒀다 — 필수에서 선택으로만 낮추고, 설명에
# "글자 파일은 원문을 넣고 base64로 바꾸지 마세요"라고 굵게 적었다. 그런데 웹에서
# `.md` 파일 하나를 올리자 붙은 AI가 **그래도 base64로 바꾸기 시작했고** 회원은
# 몇 분을 기다리다 응답을 멈췄다.
#
# 같은 결함을 이 도구에서 이미 한 번 겪었다(`body`를 필수로 뒀다가 재시도 폭주).
# 그때 얻은 것이 **"설명이 아니라 칸의 모양이 결정한다"**였는데, 첫 수정은 그것을
# 거꾸로 적용해 설명만 세게 썼다. 칸이 있으면 쓰인다.
#
# 그래서 칸을 없앤다. 글자 파일은 `content_text`, 나머지는 전부 티켓이다. 작은
# 바이너리를 AI가 직접 밀어 넣던 길이 사라지지만, 그 길은 애초에 쓸 수 없었다 —
# 50KB짜리도 base64로는 6만 8천 자라 몇 분이 걸린다.
# ---------------------------------------------------------------------------
_UPLOAD_DESCRIPTION = (
    "Upload one **text** file to this user's own GitHub repository (under "
    "attach_file/) and log it in the attachments bowl. The file goes straight "
    "to GitHub — it is never kept on this server.\n"
    "Put the text itself in `content_text`, exactly as it is. There is no "
    "base64 field on this tool and you must not create one: encoding a file "
    "into text is what used to make this take minutes.\n"
    "**Anything that is not plain text — PPT/PDF/images/video/zip — and any "
    "text over 100KB goes through `namu_create_upload_ticket` instead.** That "
    "gives a link the file is sent to directly, so its contents never pass "
    "through your output and size stops mattering.\n"
    "- `name`: the file name, e.g. '2026-3분기-보고서.md'. Sub-folders are not "
    "used; the file always lands at attach_file/<name>.\n"
    "- Uploading the same name again replaces the file on GitHub and is logged "
    "as a new revision ('새 판'); the earlier log entry stays.\n"
    "- `summary` (what this file is) and `reason` (why it was kept) are "
    "required: the file body is not synced to the user's PCs, so these two "
    "lines are what makes the file findable later.\n"
    "- `body` is optional here — for an attachment the file itself IS the full "
    "story. Add it only when there is context the file does not carry."
)


def _text_to_bytes(content_text: str) -> bytes:
    """글자 원문을 저장할 바이트로 바꾼다. 상한을 넘으면 티켓으로 안내한다."""
    if not (content_text or "").strip():
        raise ValueError(
            "content_text가 비어 있습니다 — 글자 파일의 원문을 그대로 넣어 주세요. "
            "글자가 아니거나 100KB를 넘으면 namu_create_upload_ticket을 쓰세요. "
            "Provide the text itself, or use the upload ticket."
        )
    content = content_text.encode("utf-8")
    if len(content) > attach_files.MAX_INLINE_TEXT_BYTES:
        raise ValueError(
            f"content_text가 너무 큽니다 ({len(content):,}바이트) — 이 칸의 "
            f"상한은 {attach_files.MAX_INLINE_TEXT_BYTES:,}바이트입니다. "
            "이보다 큰 파일은 namu_create_upload_ticket으로 올리세요. "
            "content_text is too large; use namu_create_upload_ticket."
        )
    return content


@mcp.tool(description=_UPLOAD_DESCRIPTION)
def namu_upload_file(
    name: str,
    content_text: str,
    summary: str,
    reason: str,
    body: str | None = None,
    topic: str | None = None,
    project: str | None = None,
    tags: "list[str] | None" = None,
    ctx: Context | None = None,
) -> dict:
    key = _resolve_user(ctx)
    via = _resolve_via(ctx)
    content = _text_to_bytes(content_text)

    meta = {
        "summary": summary, "reason": reason, "body": body,
        "topic": topic, "project": project, "tags": tags,
    }
    with closing(identity.connect()) as conn:
        out = store_file(conn, key, name, content, meta, via)

    out["note"] = (
        "`seconds`는 서버가 이 요청을 받은 뒤 걸린 시간(초)입니다. 회원이 화면에서 "
        "느낀 시간이 이보다 훨씬 길면, 느린 쪽은 서버가 아니라 파일 내용을 글자로 "
        "옮겨 보내는 과정입니다 — 회원이 물으면 이 숫자를 그대로 보여주세요."
    )
    return out


_LIST_DESCRIPTION = (
    "List the files this user has uploaded (attach_file/ in their own GitHub "
    "repository), each with its size and the note recorded when it was "
    "uploaded.\n"
    "- Names and sizes come from GitHub; why/when/which-task come from the "
    "attachments bowl.\n"
    "- `include_removed=True` also lists files that were deleted, so you can "
    "answer 'where did that file go' — a deleted file keeps its log entry with "
    "the reason it was removed."
)


@mcp.tool(description=_LIST_DESCRIPTION)
def namu_list_files(
    include_removed: bool = False,
    ctx: Context | None = None,
) -> dict:
    key = _resolve_user(ctx)
    _resolve_via(ctx)
    with closing(identity.connect()) as conn:
        _sync_or_reject(conn, key)
        in_repo = attach_files.list_in_repo(conn, key)
        paths = _paths_for_user(key)
        _ensure_fresh(paths)
        logged = attachments.load_all(paths)

    # 경로마다 마지막 기록 — 설명·작업·시각은 여기서 온다.
    latest: dict = {}
    for entry in logged:
        if entry.get("path"):
            latest[entry["path"]] = entry

    rows = []
    for f in in_repo:
        note = latest.get(f["path"], {})
        rows.append({
            "path": f["path"],
            "bytes": f.get("bytes") if f.get("bytes") is not None else note.get("bytes"),
            "status": note.get("status") or "올림",
            "summary": note.get("summary"),
            "reason": note.get("reason"),
            "task": note.get("task"),
            "project": note.get("project"),
            "timestamp": note.get("timestamp"),
        })

    if include_removed:
        present = {f["path"] for f in in_repo}
        for path, note in latest.items():
            if path in present:
                continue
            rows.append({
                "path": path,
                "bytes": note.get("bytes"),
                "status": note.get("status") or "지움",
                "summary": note.get("summary"),
                "reason": note.get("reason"),
                "task": note.get("task"),
                "project": note.get("project"),
                "timestamp": note.get("timestamp"),
            })

    rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    return {"files": rows, "count": len(rows)}


_DOWNLOAD_DESCRIPTION = (
    "Read one uploaded file back **so that you (the model) can look at its "
    "contents**. It comes straight from the user's GitHub repository, not from "
    "this server.\n"
    "**If the point is to hand the file to the user, use "
    "`namu_create_download_ticket` instead** — that gives a link the user "
    "clicks, and nothing has to pass through your output.\n"
    "- Text files up to 100KB come back as plain text in `content_text`.\n"
    "- Anything else comes back with no content at all, just a `hint` telling "
    "you to use the download ticket. Binary files are not sent through this "
    "tool by default.\n"
    "- `force_base64=True` brings a binary back as base64 anyway. Only do this "
    "when you genuinely must process the bytes yourself; it is slow.\n"
    "- `name`: the file name as shown by namu_list_files.\n"
    "- Downloads are deliberately NOT logged (the file does not change, and on "
    "the web this server cannot tell whether the user actually saved it)."
)


def fetch_file(conn: sqlite3.Connection, user_key: str, name: str) -> bytes:
    """파일 한 개를 회원 저장소에서 받아 온다 — 받기 도구와 받기 티켓의 공통 자리.

    `store_file`을 뗀 것과 같은 이유다(설계서 11절 1단계). 미연결 회원 거절과
    사본 최신화를 두 문이 각자 베끼면 한쪽만 고쳐지는 날이 온다.
    """
    _sync_or_reject(conn, user_key)
    return attach_files.download(conn, user_key, name)


@mcp.tool(description=_DOWNLOAD_DESCRIPTION)
def namu_download_file(
    name: str,
    force_base64: bool = False,
    ctx: Context | None = None,
) -> dict:
    key = _resolve_user(ctx)
    _resolve_via(ctx)
    with closing(identity.connect()) as conn:
        content = fetch_file(conn, key, name)

    path = attach_files.normalize_name(name)
    out = {"path": path, "bytes": len(content)}

    text = attach_files.as_text(path, content)
    if text is not None:
        out["content_text"] = text
        return out
    if force_base64:
        out["content_base64"] = base64.b64encode(content).decode("ascii")
        return out
    # 몸통을 싣지 않는다. `content_base64`를 None으로라도 두는 이유는, 이 칸이
    # 아예 없으면 붙은 AI가 "도구가 실패했다"로 읽고 같은 호출을 되풀이하기
    # 때문이다 — 칸은 있고 비어 있으며, 왜 비었는지가 hint에 적혀 있다.
    out["content_base64"] = None
    out["hint"] = (
        "바이너리이거나 100KB를 넘어 원문으로 전달하지 않았습니다. 회원에게 이 "
        "파일을 건네려면 namu_create_download_ticket으로 내려받기 링크를 만들어 "
        "주세요. 내용을 직접 읽어야만 하는 경우에만 force_base64=true로 다시 "
        "부르세요(느립니다). | Not sent inline (binary or over 100KB): use "
        "namu_create_download_ticket, or retry with force_base64=true."
    )
    return out


# ---------------------------------------------------------------------------
# 티켓 세 도구 — 파일 몸통이 붙은 AI의 출력을 거치지 않게 하는 우회로.
#
# 왜 필요한가는 tickets.py 첫머리에 있다(요약: base64 글자열을 AI가 한 자씩 써야
# 해서, 6KB 문서 하나에 서버는 7.4초인데 AI 쪽은 몇 분이 걸렸다).
#
# 티켓 주소를 이 서버가 직접 지어내지 않고 요청에서 끌어내는 이유: 바깥 주소를
# 담을 전용 환경변수를 새로 만들면 .env와 컨테이너 설정 두 곳에 같은 값을 적어야
# 하고, 한쪽만 하면 조용히 빈 값이 되는 사고가 이 프로젝트에서 반복됐다
# (web_auth._public_origin의 같은 판단을 그대로 재사용한다).
# ---------------------------------------------------------------------------
def _origin_for(ctx: "Context | None") -> str:
    """이 서비스의 바깥 주소(`https://호스트`). 요청이 없으면 빈 문자열."""
    req = getattr(getattr(ctx, "request_context", None), "request", None) if ctx else None
    if req is None:
        return ""
    return web_auth._public_origin(req)


_CREATE_UPLOAD_TICKET_DESCRIPTION = (
    "Create a one-time upload link for one file, for anything you should NOT "
    "push through `namu_upload_file`: binaries (PPT/PDF/images/video/zip) and "
    "anything over 100KB. Nothing is written to GitHub until a file actually "
    "arrives, so an unused link leaves no trace anywhere.\n"
    "You get back `upload_url`. Then do ONE of these:\n"
    "1. If the file is in your own workspace, POST it yourself:\n"
    "   `curl -sS -X POST -F \"file=@/path/to/file\" <upload_url>`\n"
    "   If that fails with host_not_allowed (403), the link is still alive — "
    "just hand it to the user as in step 2. Do not create another ticket.\n"
    "2. If the file is on the user's own machine, give them the `upload_url` "
    "and ask them to open it and drop the file in.\n"
    "- `name`, `summary` and `reason` mean exactly what they mean in "
    "`namu_upload_file`; they are stored with the link and written to the "
    "attachments log when the file lands.\n"
    "- The link expires in 2 hours and works once."
)


@mcp.tool(description=_CREATE_UPLOAD_TICKET_DESCRIPTION)
def namu_create_upload_ticket(
    name: str,
    summary: str,
    reason: str,
    body: str | None = None,
    topic: str | None = None,
    project: str | None = None,
    tags: "list[str] | None" = None,
    ctx: Context | None = None,
) -> dict:
    key = _resolve_user(ctx)
    via = _resolve_via(ctx)
    # 이름과 설명을 **발급 시점에** 검사한다. 미루면 회원이 파일을 다 올린
    # 다음에야 "이름이 잘못됐다"고 튕기게 되는데, 그때는 되돌릴 방법이 없다.
    path = attach_files.normalize_name(name)
    meta = normalize_meta({
        "summary": summary, "reason": reason, "body": body,
        "topic": topic, "project": project, "tags": tags,
    })
    meta["via"] = via

    with closing(identity.connect()) as conn:
        # 저장소를 연결하지 않은 회원에게 링크를 내주지 않는다 — 링크를 받아
        # 파일까지 올린 뒤에 거절당하는 것이 가장 나쁜 순서다.
        _sync_or_reject(conn, key)
        ticket = tickets.create(
            conn, key, tickets.KIND_UPLOAD, path, meta,
            ttl_sec=tickets.UPLOAD_TTL_SEC,
        )
        tickets.purge_expired(conn)

    upload_url = f"{_origin_for(ctx)}{ticket_web.UPLOAD_PREFIX}{ticket['ticket_id']}"
    return {
        "ticket_id": ticket["ticket_id"],
        "upload_url": upload_url,
        "expires_at": ticket["expires_at"],
        "name": path,
        "curl": f'curl -sS -X POST -F "file=@<파일경로>" {upload_url}',
        "if_blocked": (
            "curl이 403 host_not_allowed로 막히면 이 링크는 **그대로 살아 있습니다** "
            "— 새로 만들지 말고 회원에게 이렇게 안내하세요: (A) 파일을 이 링크에 "
            "직접 올리기, 또는 (B) claude.ai 설정 → Capabilities → Code execution의 "
            "Additional allowed domains에 이 서비스 주소를 추가한 뒤 새 대화에서 "
            "다시 시도(한 번 해두면 이후 자동)."
        ),
    }


_CREATE_DOWNLOAD_TICKET_DESCRIPTION = (
    "Create a download link for one uploaded file. **This is how you hand a "
    "file to the user** — they click the link and the file downloads straight "
    "from their GitHub repository. Nothing passes through your output.\n"
    "- Use this instead of `namu_download_file` whenever the user wants the "
    "file itself rather than you reading its contents.\n"
    "- You can also fetch it yourself with `curl -sS -o <path> <download_url>` "
    "if you need the bytes in your workspace.\n"
    "- `name`: the file name as shown by namu_list_files.\n"
    "- The link expires in 1 hour and can be opened more than once until then."
)


@mcp.tool(description=_CREATE_DOWNLOAD_TICKET_DESCRIPTION)
def namu_create_download_ticket(name: str, ctx: Context | None = None) -> dict:
    key = _resolve_user(ctx)
    via = _resolve_via(ctx)
    path = attach_files.normalize_name(name)

    with closing(identity.connect()) as conn:
        _sync_or_reject(conn, key)
        # 없는 파일에 링크를 내주지 않는다 — 회원이 눌렀을 때 비로소 깨지는
        # 링크는 "받기가 고장났다"로 읽힌다. 목록으로 존재를 먼저 확인한다
        # (목록은 이름·크기만 오므로 파일 몸통을 끌어오지 않는다).
        listed = {f["path"]: f for f in attach_files.list_in_repo(conn, key)}
        if path not in listed:
            raise ValueError(
                f"저장소에 그런 파일이 없습니다: {path} — namu_list_files로 "
                "이름을 먼저 확인해 보세요. No such file in the repository."
            )
        ticket = tickets.create(
            conn, key, tickets.KIND_DOWNLOAD, path, {"via": via},
            ttl_sec=tickets.DOWNLOAD_TTL_SEC,
        )
        tickets.purge_expired(conn)

    download_url = (
        f"{_origin_for(ctx)}{ticket_web.DOWNLOAD_PREFIX}{ticket['ticket_id']}"
    )
    return {
        "ticket_id": ticket["ticket_id"],
        "download_url": download_url,
        "name": path,
        "bytes": listed[path].get("bytes"),
        "expires_at": ticket["expires_at"],
    }


_CHECK_TICKET_DESCRIPTION = (
    "Check whether a file has arrived through an upload link yet. Use this when "
    "the user says 'I uploaded it'.\n"
    "- `status` is one of 완료 (done), 대기중 (waiting), 만료됨 (expired), 없음 "
    "(no such link).\n"
    "- A download link stays 대기중 even after the user downloads: this server "
    "cannot tell whether they actually saved the file, and does not log it."
)


@mcp.tool(description=_CHECK_TICKET_DESCRIPTION)
def namu_check_ticket(ticket_id: str, ctx: Context | None = None) -> dict:
    key = _resolve_user(ctx)
    _resolve_via(ctx)
    with closing(identity.connect()) as conn:
        ticket = tickets.get(conn, ticket_id)
    # 남의 티켓은 "없음"으로 답한다 — 있다고 알려 주면 번호를 넣어 보는 쪽에
    # 정보를 주게 된다.
    if ticket is None or ticket.get("user_key") != key:
        return {"status": tickets.STATUS_MISSING}

    out = {
        "status": tickets.status_of(ticket),
        "kind": ticket["kind"],
        "name": ticket["name"],
        "expires_at": ticket["expires_at"],
    }
    result = ticket.get("result") or {}
    if result:
        out["path"] = result.get("path")
        out["bytes"] = result.get("bytes")
    return out


_DELETE_DESCRIPTION = (
    "Remove one uploaded file from the user's GitHub repository and log why.\n"
    "- `reason` is required: the log keeps the entry after the file is gone, so "
    "'where did that file go' can be answered with when and why.\n"
    "- ⚠ This is not an erase. Git keeps history, so anyone who walks back to a "
    "commit before the deletion still finds the file. Say so plainly if the "
    "user asks for the file to be wiped — truly erasing it means rewriting the "
    "repository history, which this tool does not do."
)


@mcp.tool(description=_DELETE_DESCRIPTION)
def namu_delete_file(
    name: str,
    reason: str,
    ctx: Context | None = None,
) -> dict:
    key = _resolve_user(ctx)
    via = _resolve_via(ctx)
    if not (reason or "").strip():
        raise ValueError(
            "'reason'(왜 빼는지)은 필수입니다 — 파일은 사라져도 기록은 남으므로, "
            "나중에 '그 자료 어디 갔지'에 답할 수 있는 것은 이 한 줄뿐입니다."
        )

    with closing(identity.connect()) as conn:
        _sync_or_reject(conn, key)
        paths = _paths_for_user(key)
        _ensure_fresh(paths)
        # 마지막 기록에서 크기를 물려받는다 — 파일이 사라진 뒤에는 크기를 물어볼
        # 곳이 없고, 첨부 기록의 bytes는 비워 둘 수 없는 칸이다.
        previous = [e for e in attachments.load_all(paths) if e.get("path")]
        path = attach_files.normalize_name(name)
        last = next(
            (e for e in reversed(previous) if e.get("path") == path), {}
        )
        attach_files.delete(conn, key, name, attach_files.commit_message("지움", name))
        # 올리기와 같은 이유로 기록을 쓰기 전에 맞춘다(위 절 참고).
        _resync_after_repo_change(conn, key)

        entry_id = _record_attachment_entry(
            paths, path=path, size=last.get("bytes") or 0, status="지움",
            summary=last.get("summary") or f"{path} 를 저장소에서 뺐다",
            reason=reason,
            body=last.get("body") or "생략",
            topic=last.get("task"), project=last.get("project"), tags=None, via=via,
        )
        warning = _push_and_collect_warning(conn, key)

    out = {"id": entry_id, "path": path, "status": "지움"}
    if warning:
        out["warning"] = warning
    return out


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

    티켓 주소(`/u/…`·`/d/…`)도 웹 쪽으로 보낸다. 여기서는 접두어로 가르는데,
    위에서 공개 페이지를 `==`로만 연 것과 어긋나 보이지만 판단 근거는 같다 —
    **잘못 분류됐을 때 어느 쪽으로 새는가**다. `/u/../mcp` 같은 조작이 티켓
    앱으로 가면 거기엔 그 주소가 없어 404가 나올 뿐, MCP 도구에는 닿지 못한다.
    반대 방향(MCP 쪽으로 새는 것)만이 위험한데 접두어 검사로는 그 일이 일어나지
    않는다. 티켓은 번호가 주소 안에 있어 목록으로 열 수가 없다.
    """

    def __init__(self, auth_app, mcp_app, ticket_app):
        self.auth_app = auth_app
        self.mcp_app = mcp_app
        self.ticket_app = ticket_app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http":
            if path.startswith("/auth/") or path in _PUBLIC_PATHS:
                await self.auth_app(scope, receive, send)
                return
            if ticket_web.is_ticket_path(path):
                await self.ticket_app(scope, receive, send)
                return
        await self.mcp_app(scope, receive, send)


# ---------------------------------------------------------------------------
# 기동 엔트리포인트 — stateless HTTP, 경로는 path_secret 유무에 따라 /mcp 또는
# /mcp/<secret>. `/auth/`로 시작하는 요청은 인증 우회(로그인 전이라 토큰이 없는
# 것이 정상)로 web_auth 앱에, 그 외 전부는 기존 MCP 앱 + AuthMiddleware로 간다.
# ---------------------------------------------------------------------------
def build_ticket_app():
    """티켓 주소 세 개(`/u/…`·`/d/…`)를 담은 앱.

    길(주소·검증·실패 처리·올리는 화면)은 코어의 `ticket_web`에 있고, 이 서버는
    **자기와 다른 것만** 넘긴다 — 파일을 어디에 저장하는지(개인 주소는 이 PC의
    git, 여기는 회원 GitHub 저장소), 화면 껍데기, 로그인한 사람이 누구인지.
    베껴 두 벌로 만들면 한쪽만 고쳐지는 날이 오고, 그 한쪽은 파일이 오가는 문이다.
    """
    return ticket_web.build_ticket_app(
        open_conn=identity.connect,
        store_file=store_file,
        fetch_file=fetch_file,
        max_bytes=attach_files.max_bytes,
        session_user_key=web_auth._session_user_key,
        render_page=lambda title, body_html: ui.page(title, body_html, cta="me"),
    )


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
    return _AuthOrMcpDispatcher(auth_app, mcp_app, build_ticket_app())


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
