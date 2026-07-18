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

보안 경계(멀티테넌트 격리의 핵심)는 `_resolve_user`/`_validate_user_key`/
`_paths_for_user` 세 함수에 있다 — 키가 없거나 안전하지 않으면(경로 이탈 문자
포함) 저장/조회를 거부하고, resolve() 후 STORE_ROOT/users 밖으로 벗어나지
않는지 이중으로 재확인한다.
"""
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# vendor/namu-agent/namu-plugin을 sys.path에 얹는다 (이 파일 위치 기준 절대경로).
# vendor/namu-agent는 수정 금지 대상 — 코어는 읽기 재사용만 한다.
_VENDOR_PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent / "vendor" / "namu-agent" / "namu-plugin"
)
if str(_VENDOR_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_PLUGIN_DIR))

import config as cfg  # noqa: E402
import db  # noqa: E402
import profile  # noqa: E402
from mcp.server.fastmcp import Context, FastMCP  # noqa: E402

mcp = FastMCP("namu-cloud-routing")


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
# 3도구 — 이름·파라미터는 개인용 mcp_server.py와 동일(claude.ai 커넥터 호환).
# ---------------------------------------------------------------------------
@mcp.tool()
def namu_recall(
    query: str | None = None,
    task_type: str | None = None,
    limit: int = 5,
    ctx: Context | None = None,
):
    """Load relevant past memory for the requesting user (multi-tenant routing).

    Routes to the caller's own data directory via the `user` URL query param
    (append `?user=<your-key>` to the MCP URL). Returns the same two-bowl
    shape as the personal NAMU server: {"profile": [...active facts...],
    "learnings": [...lesson/note dicts...]}, but scoped strictly to this
    user's own memory (STORE_ROOT/users/<key>/).

    Args:
      query: topic keywords (optional; omit to get the most recent learnings)
      task_type: filter by code/doc/analysis/other (optional; learnings only)
      limit: max learnings entries (default 5)
    Returns: {"profile": [...], "learnings": [...]}
    """
    key = _resolve_user(ctx)
    paths = _paths_for_user(key)
    _ensure_fresh(paths)
    with closing(sqlite3.connect(paths.db_path)) as conn:
        return {
            "profile": profile.active(paths=paths),
            "learnings": db.recall(conn, query, task_type, limit),
        }


@mcp.tool()
def namu_search(
    query: str,
    outcome_filter: str | None = None,
    limit: int = 10,
    ctx: Context | None = None,
):
    """Search this user's accumulated learnings for patterns (exact match,
    no recency fallback). Routed via the `user` URL query param, same as
    namu_recall/namu_record.

    Args:
      query: search terms
      outcome_filter: 'success'/'failure'/'partial' to narrow returned rows (optional)
      limit: max returned rows (default 10)
    Returns: {"results": [...dicts...], "summary": {"success": N, "failure": M, "partial": K}}
    """
    key = _resolve_user(ctx)
    paths = _paths_for_user(key)
    _ensure_fresh(paths)
    with closing(sqlite3.connect(paths.db_path)) as conn:
        return db.search(conn, query, outcome_filter, limit)


@mcp.tool()
def namu_record(
    task: str | None = None,
    outcome: str | None = None,
    reason: str | None = None,
    task_type: str = "other",
    verified_by: str = "ai",
    tags: "list[str] | None" = None,
    kind: str = "lesson",
    subject: str | None = None,
    statement: str | None = None,
    source: str | None = None,
    supersedes: str | None = None,
    ctx: Context | None = None,
):
    """Record memory into this user's own bowl (append-only), routed via the
    `user` URL query param. Which bowl depends on `kind`, mirroring the
    personal NAMU server:
      - kind='lesson' (default): task outcome + reasoning, into this user's
        learnings.yaml. 'reason' and 'outcome' are mandatory.
      - kind='note': a conversation snippet, also into learnings.yaml (no
        outcome required). 'reason' still mandatory.
      - kind='fact': a fact/preference, into this user's separate
        profile.yaml bowl. Use subject/statement/source/supersedes instead
        of task/outcome/reason. 'source' is mandatory.

    Args:
      task: what was done (lesson/note only)
      outcome: 'success' | 'failure' | 'partial' (lesson: required; note: optional)
      reason: WHY (lesson/note, required, non-empty)
      task_type: code/doc/analysis/other (default 'other'; lesson/note only)
      verified_by: 'human'/'ai'/'unverified' (default 'ai')
      tags: list of string tags (optional)
      kind: 'lesson' (default) | 'note' | 'fact'
      subject: what/who this fact is about (fact only)
      statement: the fact/preference itself (fact only)
      source: WHY/how you know this is true (fact only, required, non-empty)
      supersedes: id of the prior fact entry this one corrects (fact only, optional)
    Returns: the new entry's ULID (str)
    """
    key = _resolve_user(ctx)
    paths = _paths_for_user(key)
    _ensure_fresh(paths)
    if kind in ("lesson", "note"):
        return db.record(
            task, outcome, reason, task_type, verified_by,
            _normalize_tags(tags), kind=kind, paths=paths,
        )
    elif kind == "fact":
        vb = verified_by if verified_by in ("human", "ai", "unverified") else "human"
        return profile.record_fact(
            subject, statement, source, supersedes=supersedes,
            verified_by=vb, tags=_normalize_tags(tags), paths=paths,
        )
    else:
        raise ValueError("kind는 'lesson'/'note'/'fact' 중 하나여야 합니다")


# ---------------------------------------------------------------------------
# 기동 엔트리포인트 — stateless HTTP, 고정 경로 /mcp.
# ---------------------------------------------------------------------------
def build_app():
    mcp.settings.stateless_http = True
    mcp.settings.streamable_http_path = "/mcp"
    return mcp.streamable_http_app()


def main() -> None:
    host = os.environ.get("NAMU_HTTP_HOST", "127.0.0.1").strip()
    port_raw = os.environ.get("NAMU_HTTP_PORT", "8770").strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError(f"NAMU_HTTP_PORT 값이 정수가 아닙니다: {port_raw!r}") from exc

    app = build_app()

    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
