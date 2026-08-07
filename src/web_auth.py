"""GitHub OAuth 사용자 로그인 흐름 (사용자 신원 계층 2차).

1차(github_app.py/identity.py)가 "나무 서버가 그 앱임을 증명하는" 서버 대 서버
인증을 끝냈다면, 이 모듈은 "이 브라우저를 쥔 사람이 누구인지"를 알아내는
사용자 대 서버 인증(OAuth)이다. 라우트 9개(`/auth/github/login` →
`/auth/github/callback` → 앱 설치가 필요하면 `/auth/github/install`을 거쳐
다시 `/auth/github/callback` → 저장소가 여럿이면 `/auth/github/select-repo`)로
로그인·연결이 끝나고, 그 뒤로 언제든 `/auth/me`(내 페이지)로 접속 주소를 다시
볼 수 있다. 내 페이지에서는 주소를 시험(`/auth/mcp/test`)·재발급
(`/auth/mcp/rotate`)·폐기(`/auth/mcp/revoke`)할 수 있다 — 이 셋은 POST 전용이다
(파괴적 동작이라 링크 클릭·프리페치로 실행되면 안 된다). `/auth/logout`은 세션
쿠키를 지운다.

이 서비스가 발급하는 MCP 접속 주소는 **웹 AI(claude.ai 등) 전용**이다 —
Claude Code·agy 사용자는 이 주소를 붙이는 것이 아니라 나무를 플러그인으로
설치한다(주소로 넘어가는 것은 기억 3종과 첨부 7종뿐이고 세션 브리핑·작업 절차·
마무리 훅이 따라오지 않는다). 그 구분을 화면에서 알리는 곳이
`_html_onboarding_section`이다.

설계 전제(핵심 — routing_server.py/github_app.py 모듈 docstring과 동일한 원칙):
  - 사용자 access token은 **저장하지 않는다**. 콜백 요청 처리 중 지역 변수로만
    쓰고 응답을 만들면 버려진다 — 영구 열쇠를 보관하지 않는 것이 이 설계
    전체의 존재 이유다.
  - 로그인 자체는 "신원 확인"일 뿐 저장소 접근권을 주지 않는다. 저장소 접근권은
    사용자가 별도로 앱을 설치(installation)하고 repo를 고를 때만 생긴다.
  - 연결 완료 그 순간의 화면 하나로만 접속 주소를 볼 수 있었던 것(namu-60
    이전)은 창을 닫으면 다시 볼 경로가 없는 결함이었다 — `/auth/me`가 그
    영구 경로다(namu-60).

쿠키 서명 규약(자체 구현, 외부 라이브러리 미사용):
  `value.hexhmac` 형태(HMAC-SHA256, `NAMU_SESSION_SECRET` 키). 만료가 필요한
  값(state, session)은 서명 전에 `"<만료epoch>|<원래값>"`으로 감싸 서버가 직접
  만료를 강제한다(Max-Age는 클라이언트가 지키는 값이라 신뢰하지 않는다).
  `NAMU_HTTP_TOKEN`(MCP 헤더 인증 토큰)과 이 시크릿을 절대 겸용하지 않는다 —
  하나를 교체할 때 다른 하나가 함께 무효화되는 결합을 피하기 위해서다.

환경변수는 github_app.py/identity.py와 동일하게 전부 **호출 시점에 읽는다**
(모듈 로드 시점 상수로 굳히지 않는다 — 테스트가 monkeypatch.setenv로 격리해야
한다).
"""
import hashlib
import hmac
import html
import json
import logging
import math
import os
import secrets
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

import github_app as ga
import identity
import pages
import ui
import user_repo

logger = logging.getLogger("namu.web_auth")

# httpx는 매 요청의 URL을 INFO로 찍는다("HTTP Request: POST http://... 200 OK").
# 연결 시험(`_http_probe`)은 URL 경로에 사용자 접속 열쇠를 실어 보내므로, 그대로
# 두면 우리 로그가 곧 열쇠 유출 경로가 된다(실측 확인 — routing_server가 열쇠를
# 로그에 남기지 않는 것과 같은 원칙). 한 단계 올려 그 줄만 끈다.
logging.getLogger("httpx").setLevel(logging.WARNING)

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_USER_INSTALLATIONS_URL = "https://api.github.com/user/installations"
GITHUB_USER_INSTALLATIONS_REPOS_URL_TMPL = (
    "https://api.github.com/user/installations/{iid}/repositories"
)
GITHUB_INSTALL_URL_TMPL = "https://github.com/apps/{slug}/installations/new"

_STATE_COOKIE_NAME = "namu_oauth_state"
_SESSION_COOKIE_NAME = "namu_session"
# state는 로그인 왕복(브라우저→GitHub→콜백) 동안만 살아있으면 된다 — 짧을수록
# CSRF에 악용될 수 있는 창이 좁아진다.
_STATE_COOKIE_TTL_SEC = 600
# 콜백 직후 저장소가 2개 이상이라 select-repo로 한 번 더 왕복해야 하는 경우까지
# 감안한 여유(사용자가 화면을 보고 고르는 시간 포함).
_SESSION_COOKIE_TTL_SEC = 1800

_DEFAULT_APP_SLUG = "namu-memory-app"

_HTTP_TIMEOUT_SEC = 15.0


# ---------------------------------------------------------------------------
# 환경변수 (지연 평가)
# ---------------------------------------------------------------------------
def _required_env(name: str, what: str) -> str:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} 환경변수가 설정되지 않았습니다 — {what}")
    return raw


def _session_secret() -> bytes:
    """쿠키 서명·링크 서명 전용 키. NAMU_HTTP_TOKEN과 값을 공유하지 않는다.

    (재사용하면 MCP 접근 토큰을 교체할 때 로그인 중인 모든 세션·state 쿠키가
    함께 무효화되고, 반대로 세션 시크릿을 교체하면 MCP 인증이 끊긴다 — 서로
    다른 수명 주기를 갖는 값을 하나로 묶으면 안 된다.)
    """
    return _required_env(
        "NAMU_SESSION_SECRET",
        "쿠키 서명(state/session)과 select-repo 링크 서명에 쓸 임의의 강한 문자열을 "
        "지정하세요. NAMU_HTTP_TOKEN(MCP 헤더 인증 토큰)과 같은 값을 쓰지 마세요 — "
        "하나를 교체하면 다른 하나도 함께 무효화됩니다. "
        "Missing NAMU_SESSION_SECRET: set a strong random string, distinct from "
        "NAMU_HTTP_TOKEN.",
    ).encode("utf-8")


def _app_slug() -> str:
    """설치 링크(`github.com/apps/<slug>/installations/new`) 조립용."""
    return os.environ.get("NAMU_GITHUB_APP_SLUG", "").strip() or _DEFAULT_APP_SLUG


# ---------------------------------------------------------------------------
# 서명 — HMAC-SHA256, 값 자체는 로그/예외에 남기지 않는다(서명 실패도 이유만
# 알리고 후보 값은 담지 않는다).
# ---------------------------------------------------------------------------
def _hmac_hex(payload: str) -> str:
    return hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _sign(value: str) -> str:
    return f"{value}.{_hmac_hex(value)}"


def _unsign(signed: "str | None") -> "str | None":
    if not signed or "." not in signed:
        return None
    value, _, mac = signed.rpartition(".")
    if not hmac.compare_digest(mac, _hmac_hex(value)):
        return None
    return value


def _sign_with_expiry(payload: str, ttl_seconds: int) -> str:
    expires_at = int(time.time()) + ttl_seconds
    return _sign(f"{expires_at}|{payload}")


def _unsign_with_expiry(signed: "str | None") -> "str | None":
    """검증 + 만료 확인. 서명이 틀렸거나 기한이 지났으면 이유를 구분하지 않고
    None을 돌려준다 — 호출부가 어느 쪽이든 "다시 로그인하라"로 처리하면 된다."""
    raw = _unsign(signed)
    if raw is None or "|" not in raw:
        return None
    expires_raw, _, payload = raw.partition("|")
    try:
        expires_at = int(expires_raw)
    except ValueError:
        return None
    if time.time() > expires_at:
        return None
    return payload


def _repo_link_sig(user_key: str, installation_id: int, repo: str) -> str:
    """콜백이 조회한 저장소 목록을 select-repo 링크에 실을 때 붙이는 서명.

    user_key까지 서명 대상에 포함하는 이유: repo/installation_id만 서명하면,
    한 사용자에게 발급된 select-repo 링크(예: 로그가 유출됐거나 화면을 같이
    본 경우)를 다른 로그인 세션이 그대로 재사용해 "남의 installation을 내
    계정에 연결"할 수 있다 — installation_id는 어느 GitHub 계정이 설치했는지와
    묶여 있으므로, 그 결과 나무가 다른 사람의 repo에 접근할 때 엉뚱한 사용자의
    토큰 경로로 라우팅되는 사고가 된다. user_key를 서명에 묶으면 이 링크는
    발급받은 세션 본인만 소비할 수 있다.
    """
    payload = f"{user_key}:{installation_id}:{repo}"
    return _hmac_hex(payload)


# ---------------------------------------------------------------------------
# HTTP — 이 모듈의 유일한 네트워크 접점. 테스트는 이 함수 하나만 monkeypatch하면
# 로그인→콜백→저장소 선택 전 흐름을 네트워크 없이 검증할 수 있다
# (github_app._post_json과 같은 설계 원칙).
#
# github_app._post_json과 달리 상태코드로 성패를 가르지 않고 (status, json)을
# 그대로 돌려준다 — GitHub 토큰 교환 엔드포인트(`/login/oauth/access_token`)는
# 실패해도 HTTP 200에 `{"error": ...}`로 응답하므로, 판정은 반드시 응답 본문을
# 본 호출부가 내려야 한다.
# ---------------------------------------------------------------------------
def _http_json(
    method: str, url: str, *, headers: "dict | None" = None, json_body: "dict | None" = None
) -> "tuple[int, dict]":
    import httpx

    resp = httpx.request(method, url, headers=headers or {}, json=json_body, timeout=_HTTP_TIMEOUT_SEC)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return resp.status_code, (data if isinstance(data, dict) else {})


def _bearer_headers(user_token: str) -> dict:
    return {
        "Authorization": f"Bearer {user_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _exchange_code_for_token(code: str) -> str:
    """authorization code를 사용자 access token으로 교환한다.

    확신 없는 지점(GitHub API 동작을 기억으로 단정): token 엔드포인트가 JSON
    본문(Content-Type: application/json)도 accept하는지는 공식 문서 기억에
    기반한 가정이며, 실제 호출로 확인하지 못했다 — 실패 시 대안은 폼 인코딩
    (application/x-www-form-urlencoded)으로 바꾸는 것이다.
    """
    status, data = _http_json(
        "POST",
        GITHUB_TOKEN_URL,
        headers={"Accept": "application/json"},
        json_body={
            "client_id": ga.client_id(),
            "client_secret": ga.client_secret(),
            "code": code,
        },
    )
    # GitHub은 실패해도 HTTP 200 + {"error": ...}로 응답할 수 있으므로 status만
    # 보면 안 된다 — 본문의 error 키를 반드시 함께 검사한다.
    error = data.get("error")
    if error or status >= 400:
        raise ValueError(
            f"GitHub 로그인 승인 교환에 실패했습니다 (error={error or 'unknown'}, "
            f"status={status}) — 처음부터 다시 로그인해 주세요. "
            "GitHub OAuth code exchange failed."
        )
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError(
            "GitHub 응답에 access_token이 없습니다. "
            "Missing access_token in GitHub response."
        )
    return token


def _fetch_github_user(user_token: str) -> dict:
    status, data = _http_json("GET", GITHUB_USER_URL, headers=_bearer_headers(user_token))
    if status >= 400:
        raise RuntimeError(f"GitHub 사용자 정보 조회에 실패했습니다 (status={status}).")
    return data


# 페이지당 최대(GitHub 허용 상한)로 받아 왕복 횟수를 줄인다. 안전 상한
# (page cap)은 무한 루프 방어이자 "이 개수를 넘는 계정은 실제로 없을 것"이라는
# 낙관이 아니라 최후의 방어선이다 — 도달하면 반드시 화면에 알린다(아래
# `_fetch_installation_repos`의 반환값 두 번째 요소 `truncated` 참고).
_INSTALLATION_REPOS_PER_PAGE = 100
_INSTALLATION_REPOS_MAX_PAGES = 50  # per_page(100) 기준 최대 5000개까지 수집


def _fetch_user_installations(user_token: str) -> "tuple[list[int], bool]":
    """이 사용자가 **이미 설치해 둔** installation id 목록을 전부 모은다.

    GitHub은 설치가 새로 일어난 왕복에만 콜백에 `installation_id`를 실어준다.
    이미 설치한 사용자는 설치 링크가 설치 화면이 아니라 설정 화면으로 넘어가고,
    바꿀 것이 없으면 Save 버튼이 비활성이라 우리 서버로 되돌아오는 왕복 자체가
    생기지 않는다(2026-07-26 실측). 그 경우 콜백은 설치 번호를 알 길이 없으므로
    여기서 사용자 토큰으로 직접 조회한다.

    응답 스키마(`total_count`/`installations`, 각 항목의 `id`)와 페이지네이션
    기본값은 GitHub 공식 OpenAPI 스펙(`GET /user/installations`) 기준이다.
    반환값 `(ids, truncated)`의 truncated 의미는 `_fetch_installation_repos`와 같다.
    """
    ids: list[int] = []
    total_count: "int | None" = None
    truncated = False
    page = 1
    while True:
        url = (
            f"{GITHUB_USER_INSTALLATIONS_URL}?"
            f"{urlencode({'per_page': _INSTALLATION_REPOS_PER_PAGE, 'page': page})}"
        )
        status, data = _http_json("GET", url, headers=_bearer_headers(user_token))
        if status >= 400:
            raise RuntimeError(
                f"GitHub 설치 목록 조회에 실패했습니다 (status={status})."
            )
        if total_count is None:
            raw_total = data.get("total_count")
            if isinstance(raw_total, int):
                total_count = raw_total

        installs_raw = data.get("installations")
        page_ids = []
        if isinstance(installs_raw, list):
            for item in installs_raw:
                if isinstance(item, dict) and isinstance(item.get("id"), int):
                    page_ids.append(item["id"])
        ids.extend(page_ids)

        if not page_ids:
            break
        if total_count is not None and len(ids) >= total_count:
            break
        if page >= _INSTALLATION_REPOS_MAX_PAGES:
            truncated = True
            break
        page += 1
    return ids, truncated


def _fetch_installation_repos(user_token: str, installation_id: int) -> "tuple[list[str], bool]":
    """installation이 허용한 저장소들의 `owner/repo` 목록을, 페이지네이션을 끝까지
    따라가며 전부 모은다.

    응답 스키마(`total_count`/`repositories`, 각 저장소의 `full_name`)와 페이지네이션
    기본값(`per_page=30`, `page=1`)은 GitHub 공식 OpenAPI 스펙
    (github/rest-api-description repo의 api.github.com.json,
    `GET /user/installations/{installation_id}/repositories`)으로 확정된 사실이다
    — 기억에 의존한 추정이 아니다.

    반환값 `(names, truncated)`: `truncated=True`면 안전 상한
    (`_INSTALLATION_REPOS_MAX_PAGES` 페이지)에 걸려 전부 못 가져왔다는 뜻이다.
    설치 시 "All repositories"를 고른 계정이 저장소를 30개(GitHub page 기본값)
    넘게 갖고 있으면 첫 페이지만 받고 끝나 뒤쪽이 에러 없이 조용히 누락되는
    결함이 있었다(실제 코드 결함으로 지적됨) — 그래서 이 함수는 반드시 페이지네이션
    전체를 따라가고, 그래도 안전 상한에 걸리면 그 사실을 truncated로 알려 호출부가
    "조용히 자르는" 대신 화면에 경고를 낼 수 있게 한다.
    """
    names: list[str] = []
    total_count: "int | None" = None
    truncated = False
    base_url = GITHUB_USER_INSTALLATIONS_REPOS_URL_TMPL.format(iid=installation_id)
    page = 1
    while True:
        url = f"{base_url}?{urlencode({'per_page': _INSTALLATION_REPOS_PER_PAGE, 'page': page})}"
        status, data = _http_json("GET", url, headers=_bearer_headers(user_token))
        if status >= 400:
            raise RuntimeError(
                f"GitHub installation 저장소 목록 조회에 실패했습니다 (status={status})."
            )
        if total_count is None:
            raw_total = data.get("total_count")
            if isinstance(raw_total, int):
                total_count = raw_total

        repos_raw = data.get("repositories")
        page_names = []
        if isinstance(repos_raw, list):
            for item in repos_raw:
                if isinstance(item, dict) and isinstance(item.get("full_name"), str):
                    page_names.append(item["full_name"])
        names.extend(page_names)

        if not page_names:
            # 빈 페이지 = 더 이상 없음. total_count를 못 받았거나 못 믿을 때도
            # 이 조건 하나로 루프가 반드시 끝난다(무한 루프 방어의 1차 방어선).
            break
        if total_count is not None and len(names) >= total_count:
            break
        if page >= _INSTALLATION_REPOS_MAX_PAGES:
            # 2차 방어선(안전 상한) — total_count가 비정상적으로 크거나 응답이
            # 계속 채워진 페이지만 주는 경우에도 여기서 반드시 멈춘다.
            truncated = True
            break
        page += 1
    return names, truncated


# ---------------------------------------------------------------------------
# 화면 — 최소 인라인 HTML. 외부 CSS/JS/CDN 없음.
#
# 연결 완료 화면은 **완성된 MCP 접속 주소를 그대로 보여준다**(namu-59). 이전에는
# "별도 대시보드에서 확인하세요"라고만 적어 두었는데, 그 대시보드(namu-60)가
# 아직 없어서 사용자가 주소를 조립할 방법이 없었다 — 서버가 아는 값을 안 알려
# 주고 사용자더러 찾아오라고 하는 상태였다. 사용자별 열쇠로 바뀐 지금은 그
# 값이 그 사람 전용이므로 본인 화면에 띄워도 안전하다(예전 공용 열쇠였다면
# 로그인한 사람 전원에게 남의 서랍 여는 열쇠를 나눠주는 셈이라 불가능했다).
# ---------------------------------------------------------------------------
# 껍데기와 차림새는 전부 `ui.py`가 갖는다(namu-70). 여기에는 얇은 이름만 남긴다 —
# 이 파일 안에서 `_html_page`를 부르는 자리가 열세 곳이라 이름을 유지하는 편이
# 갈아끼우는 변경분을 작게 만든다.
#
# 화면 조각을 만드는 함수들은 `ui.SITE_CSS`가 정의한 클래스(.btn/.card/.step)를
# 쓴다. 인라인 style로 같은 값을 다시 적지 않는다 — 그렇게 흩어져 있던 열다섯
# 군데가 서로 조금씩 달라져, 버튼 하나를 고치면 다른 버튼이 안 따라오는 상태였다.
# ---------------------------------------------------------------------------
def _html_page(title: str, body_html: str, *, cta: str = "me") -> str:
    """로그인 뒤 화면의 껍데기.

    `cta`는 메뉴 오른쪽 끝 버튼이다 — 기본값은 '내 페이지'(로그인한 사람이
    돌아갈 곳)이고, 로그아웃·로그인 안내처럼 세션이 없는 화면만 '시작하기'로
    바꾼다. 없는 세션으로 내 페이지에 보내면 그 자리에서 튕긴다.
    """
    return ui.page(title, body_html, cta=cta)


def _public_origin(request: Request) -> str:
    """이 서비스의 바깥 주소(`https://호스트`)를 요청에서 알아낸다.

    전용 환경변수를 새로 만들지 않는 이유가 있다 — 이 배포는 값을 .env에 넣는
    것과 컨테이너에 전달되는 것이 별개라(docker-compose의 environment 블록에
    같은 이름을 또 적어야 한다) 한쪽만 하면 조용히 빈 값이 되는 사고가 이
    프로젝트에서 반복됐다. 요청에서 끌어내면 그 배선 자체가 필요 없다.

    Cloudflare 터널 뒤라 원래 scheme이 http로 보일 수 있으므로 프록시가 붙여
    주는 x-forwarded-proto를 우선한다.
    """
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    scheme = forwarded_proto if forwarded_proto in ("http", "https") else request.url.scheme
    host = (
        (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        or (request.headers.get("host") or "").strip()
        or request.url.netloc
    )
    return f"{scheme}://{host}"


def _mcp_url_for(request: Request, conn, user_key: str) -> "str | None":
    """그 사용자의 완성된 MCP 접속 주소. 열쇠가 아직 없으면 None."""
    row = identity.get_by_user_key(conn, user_key)
    mcp_secret = (row or {}).get("mcp_secret")
    if not mcp_secret:
        return None
    return f"{_public_origin(request)}/mcp/{mcp_secret}?client=claude"


# 복사 버튼 스크립트. 외부 스크립트 없음. navigator.clipboard는 보안 컨텍스트
# (https)에서만 있으므로, 없으면 execCommand로 물러난다. 연결 완료 화면과 내
# 페이지(namu-60)가 같은 UX를 써야 하므로 공통 조각으로 뽑았다 — 두 화면이
# 동시에 열리는 경우가 없어 id 중복은 문제되지 않는다.
_MCP_URL_COPY_SCRIPT = (
    "<script>"
    "document.getElementById('copy-btn').addEventListener('click',function(){"
    "var t=document.getElementById('mcp-url');t.select();"
    "var done=function(ok){document.getElementById('copy-msg').textContent="
    "ok?'복사했습니다':'복사하지 못했습니다 — 직접 선택해 복사하세요';};"
    "if(navigator.clipboard&&navigator.clipboard.writeText){"
    "navigator.clipboard.writeText(t.value).then(function(){done(true);},"
    "function(){done(false);});}else{try{done(document.execCommand('copy'));}"
    "catch(e){done(false);}}});"
    "</script>"
)


def _html_mcp_url_section(mcp_url: str) -> str:
    """접속 주소 + 복사 버튼 + 경고 문구 블록. 연결 완료 화면과 내 페이지가
    공유한다(중복 붙여넣기 대신 한 곳만 고치면 양쪽에 반영되게)."""
    safe_url = html.escape(mcp_url)
    return (
        '<div class="card">'
        "<h2 style=\"margin-top:0\">접속 주소</h2>"
        "<p>아래 주소를 복사해 사용하는 AI의 커넥터에 붙여 넣으세요.</p>"
        f'<textarea id="mcp-url" readonly rows="3" onclick="this.select()" '
        f'style="width:100%;font-family:monospace;font-size:13px;">'
        f"{safe_url}</textarea>"
        '<div class="btn-row">'
        '<button type="button" id="copy-btn" class="btn btn-primary">'
        "주소 복사</button>"
        '<span id="copy-msg"></span></div>'
        "<p><small>이 주소가 회원님의 신분증입니다 — <b>남에게 알려주지 "
        "마세요.</b> 아는 사람은 회원님 기억을 읽고 쓸 수 있습니다.</small></p>"
        "<p style=\"margin-bottom:0\"><small>클로드가 아닌 다른 AI에 붙일 때는 "
        "주소 끝의 <code>client=claude</code>를 그 AI 이름으로 바꾸세요"
        "(예: <code>client=chatgpt</code>). 나중에 '어느 AI가 남긴 기억인지' "
        "골라 찾을 때 쓰는 이름표라, 한 번 정하면 계속 같은 값을 쓰셔야 "
        "합니다.</small></p></div>" + _MCP_URL_COPY_SCRIPT
    )


# 다른 길 안내 문서(경로 B 셀프호스팅 / 플러그인 설치). 저장소 밖 문서라
# 상수로 모아 둔다 — 두 화면에서 같은 링크를 쓴다.
# 주소는 ui가 한 곳에서 정한다 — 여기에 또 적어 두면 한쪽만 고쳐지고 사이트
# 안에서 같은 문서로 가는 링크가 두 갈래로 갈린다(실제로 그렇게 갈려 있었다:
# 꼬리말은 새 안내서, 이 화면은 옛 마크다운 표지판).
_REMOTE_MCP_GUIDE_URL = ui.SELFHOST_GUIDE_URL
_INSTALL_GUIDE_URL = ui.INSTALL_GUIDE_URL


def _html_onboarding_section(mcp_url: str) -> str:
    """접속 주소 + "이걸로 뭘 하면 되는지" 안내 한 덩어리.

    연결 완료 화면(`_html_connected`)과 내 페이지(`_html_me_connected`)가
    **같은 내용을 보여야** 한다 — 완료 화면에서만 설명하면 창을 닫은 사람은
    다시 볼 방법이 없고, 두 곳에 따로 적으면 한쪽만 고쳐지는 사고가 난다
    (`_html_mcp_url_section`을 공통으로 뽑은 것과 같은 이유).

    3·4번(셀프호스팅 / Claude Code·agy)은 대부분의 사용자에게 해당되지 않아
    본문을 길게 만들면 정작 읽어야 할 2번을 밀어낸다 — `<details>`로 접어 둔다.
    2번은 접지 않는다(이 화면에 온 사람이 지금 당장 해야 할 일이다).
    """
    return (
        _html_mcp_url_section(mcp_url)
        + '<div class="card card-soft">'
        '<h2 style="margin-top:0">웹 AI에 붙이는 법 (클로드 기준)</h2>'
        "<ol>"
        "<li>claude.ai에 로그인한 뒤 <b>설정(Settings)</b>을 엽니다.</li>"
        "<li><b>커넥터(Connectors)</b> 항목으로 들어갑니다.</li>"
        "<li><b>사용자 정의 커넥터 추가(Add custom connector)</b>를 누릅니다.</li>"
        "<li>위에서 복사한 주소를 그대로 붙여 넣고 저장합니다. 이름은 아무거나 "
        "(예: 나무) 적으셔도 됩니다.</li>"
        "</ol>"
        '<p style="margin-bottom:0">붙이고 나면 대화 중에 '
        "<code>namu_recall</code>(기억 꺼내기)·"
        "<code>namu_record</code>(기억 남기기)·<code>namu_search</code>"
        "(기억 찾기) 세 가지를 쓸 수 있습니다.</p></div>"
        "<details>"
        "<summary>직접 서버를 띄우고 싶다면</summary>"
        "<p>나무는 회원님이 <b>직접 서버를 올려 쓰는 길</b>도 있습니다. 차이는 "
        "하나입니다 — 서버를 회원님이 직접 올리고 관리하느냐(직접 운영), 나무가 "
        "대신 맡느냐(지금 이 화면).</p>"
        f'<p><a href="{_REMOTE_MCP_GUIDE_URL}" target="_blank" rel="noopener">'
        "직접 서버 띄우기 안내서 열기</a></p>"
        "</details>"
        "<details>"
        "<summary>Claude Code·agy를 쓰신다면</summary>"
        "<p>그 경우에는 <b>이 주소를 붙이는 것이 아니라 나무를 플러그인으로 "
        "설치</b>하셔야 합니다. 이 주소로 넘어가는 것은 기억과 파일뿐이고, 세션 "
        "브리핑·작업 절차(<code>/namu-task</code>)·마무리 점검처럼 나무의 나머지 "
        "절반이 따라오지 않기 때문입니다.</p>"
        f'<p><a href="{_INSTALL_GUIDE_URL}" target="_blank" rel="noopener">'
        "플러그인 설치 안내서 열기</a></p>"
        "</details>"
    )


def _html_connected(user_key: str, repo_full_name: str, mcp_url: "str | None" = None) -> str:
    body = [
        ui.stepper(4, label="연결 완료"),
        '<span class="eyebrow">🎉 다 됐습니다</span>',
        "<h1>기억을 담을 자리가 마련됐습니다</h1>",
        '<p class="lead">이제부터 <code>'
        f"{html.escape(repo_full_name)}</code> 저장소가 회원님 기억의 "
        "원본입니다. 남은 일은 아래 주소를 AI에 한 번 붙이는 것뿐입니다.</p>",
    ]
    if mcp_url:
        body.append(_html_onboarding_section(mcp_url))
    else:
        # 접속 열쇠가 없는 상태(이관 실패 등). 조용히 빈 화면을 내지 않는다.
        body.append(
            _html_notice(
                "<b>접속 주소를 만들지 못했습니다.</b> 로그아웃 후 다시 로그인해 "
                "주세요. 계속 같은 화면이 나오면 관리자에게 알려주세요.",
                tone="bad",
            )
        )
    body.append(
        '<div class="btn-row">'
        '<a class="btn btn-primary" href="/auth/me">내 페이지로 이동</a>'
        '<a class="btn" href="/memory">무엇을 기억하는지 보기</a>'
        "</div>"
    )
    body.append(f"<p><small>사용자 키: <code>{html.escape(user_key)}</code></small></p>")
    return _html_page("NAMU 연결 완료", "".join(body))


def _html_select_repo(
    user_key: str, installation_id: int, repos: "list[str]", *, truncated: bool = False
) -> str:
    items = []
    for repo in repos:
        sig = _repo_link_sig(user_key, installation_id, repo)
        qs = urlencode({"installation_id": installation_id, "repo": repo, "sig": sig})
        items.append(
            f'<li><a href="/auth/github/select-repo?{qs}">{html.escape(repo)}</a></li>'
        )
    warning = ""
    if truncated:
        # 조용한 누락이 결함의 본질이었으므로, 안전 상한에 걸려 전부 못 가져온
        # 경우는 반드시 화면에서 알린다(조용히 자르는 대안으로 바꾸지 않는다).
        warning = (
            '<p style="color:var(--danger);"><strong>주의(Notice)</strong>: 저장소 목록이 너무 '
            "많아 전부 불러오지 못했습니다(안전 상한 도달) — 찾는 저장소가 아래 목록에 "
            "없다면 GitHub 설치 설정에서 허용 저장소 범위를 좁히거나 관리자에게 "
            "문의하세요. The repository list was truncated (safety limit reached); "
            "some repositories may be missing below.</p>"
        )
    body = (
        ui.stepper(3, label="저장소 고르기")
        + "<h1>어느 저장소에 기억을 담을까요?</h1>"
        + warning
        + '<p class="lead">앱을 설치하며 여러 저장소를 허용하셨습니다. '
        "기억을 담을 저장소를 하나만 고르세요.</p>"
        f'<div class="card"><ul class="repo-list">{"".join(items)}</ul></div>'
    )
    return _html_page("NAMU 저장소 선택", body)


def _html_select_repo_multi(
    user_key: str,
    pairs: "list[tuple[int, str]]",
    *,
    truncated: bool = False,
) -> str:
    """설치가 여러 개인 계정용 — `(installation_id, repo)` 쌍 목록을 한 화면에 모은다.

    개인 계정과 조직에 각각 설치한 경우처럼 설치가 둘 이상이면 저장소 이름만으로는
    어느 설치를 거쳐야 하는지 알 수 없다. 링크마다 그 저장소가 속한 설치 번호를
    실어 보내야 select-repo의 서명 검증이 통과한다.
    """
    items = []
    for installation_id, repo in pairs:
        sig = _repo_link_sig(user_key, installation_id, repo)
        qs = urlencode({"installation_id": installation_id, "repo": repo, "sig": sig})
        items.append(
            f'<li><a href="/auth/github/select-repo?{qs}">{html.escape(repo)}</a></li>'
        )
    warning = ""
    if truncated:
        warning = (
            '<p style="color:var(--danger);"><strong>주의(Notice)</strong>: 목록이 너무 많아 '
            "전부 불러오지 못했습니다(안전 상한 도달) — 찾는 저장소가 아래에 없다면 "
            "GitHub 설치 설정에서 허용 범위를 좁히세요. The list was truncated "
            "(safety limit reached); some repositories may be missing below.</p>"
        )
    body = (
        ui.stepper(3, label="저장소 고르기")
        + "<h1>어느 저장소에 기억을 담을까요?</h1>"
        + warning
        + '<p class="lead">이미 설치된 앱에서 접근할 수 있는 저장소를 모두 '
        "찾았습니다. 기억을 담을 저장소를 하나만 고르세요.</p>"
        f'<div class="card"><ul class="repo-list">{"".join(items)}</ul></div>'
    )
    return _html_page("NAMU 저장소 선택", body)


def _html_no_repos(installation_id: int) -> str:
    settings_url = f"https://github.com/settings/installations/{installation_id}"
    body = (
        ui.stepper(3, label="저장소 고르기")
        + "<h1>고를 저장소가 없습니다</h1>"
        + '<p class="lead">앱 설치는 끝났지만 접근을 허용한 저장소가 '
        "하나도 없습니다. 저장소를 만들거나, 이미 있는 저장소를 허용 목록에 "
        "넣어 주세요.</p>"
        + ui.steps(
            [
                (
                    "저장소가 아직 없다면 만드세요",
                    f'<p><a class="btn btn-primary" href="{pages.NEW_REPO_URL}" '
                    'target="_blank" rel="noopener">GitHub에서 만들기 ↗</a></p>'
                    "<p>이름(<code>namu-memory</code>)과 비공개가 미리 채워진 채로 "
                    "열립니다 — 만들기 단추 한 번이면 됩니다.</p>",
                ),
                (
                    "그 저장소를 나무에 허용하세요",
                    f'<p><a class="btn" href="{html.escape(settings_url)}" '
                    'target="_blank" rel="noopener">GitHub 설치 설정 열기 ↗</a></p>'
                    "<p>허용한 뒤 이 화면으로 돌아와 새로고침하시면 목록에 "
                    "나타납니다.</p>",
                ),
            ]
        )
    )
    return _html_page("NAMU 저장소 없음", body)


# ---------------------------------------------------------------------------
# 2단계 — 기억 저장소 마련하기 (namu-70)
#
# 이 화면이 이번 작업의 심장이다. 예전 "다음 단계" 화면은 **순서가 뒤집혀** 있었다:
# 눈에 띄는 링크가 [앱 설치]였고, "저장소가 없으면 만드세요"는 그 아래 딸린
# 문장이었다. 그런데 앱 설치 화면이 곧 "어느 저장소에 접근을 허용할지 고르는
# 화면"이다 — 저장소가 없는 사람이 먼저 그리로 가면 고를 것이 없는 화면을 만나고
# 돌아오는 길도 없었다. **저장소가 먼저, 권한이 나중이다.**
#
# 새로 만드는 것은 [만들었어요, 다음] 버튼 하나다. 미리 채워진 저장소 생성
# 링크는 namu-58부터 이미 있었고(사이트가 대신 만들지 않는 이유는 pages.py의
# 안전 페이지에 적혀 있다), 없던 것은 **만들고 돌아온 사람이 이어갈 길**뿐이다.
# ---------------------------------------------------------------------------
_INSTALL_PATH = "/auth/github/install"


def _html_repo_step() -> str:
    return _html_page(
        "NAMU 기억 저장소 마련하기",
        ui.stepper(2, label="기억 저장소")
        + "<h1>기억을 담을 저장소를 마련합니다</h1>"
        + '<p class="lead">방금 지나온 화면은 <b>회원님이 누구인지만</b> '
        "확인했습니다. 저장소 권한은 아직 아무것도 드리지 않았습니다 — "
        "그건 다음 단계에서 따로 여쭙습니다.</p>"
        + "<details><summary>저장소가 뭔가요?</summary>"
        "<p>회원님 GitHub 안의 폴더 하나입니다. 나무가 남기는 기억이 전부 그 "
        "안에 글자 파일로 쌓입니다. 원본은 회원님 것이고, 나무는 그 사본을 두고 "
        "읽고 씁니다.</p></details>"
        + '<div class="card">'
        '<h2 style="margin-top:0">새로 만들기 <span class="pill">권장</span></h2>'
        + ui.steps(
            [
                (
                    "GitHub에서 만드세요",
                    f'<p><a class="btn btn-primary" href="{pages.NEW_REPO_URL}" '
                    'target="_blank" rel="noopener">GitHub에서 만들기 ↗</a></p>'
                    "<p>이름(<code>namu-memory</code>)과 <b>비공개</b>가 미리 "
                    "채워진 채로 새 탭에 열립니다 — 만들기 단추 한 번만 "
                    "누르세요.</p>",
                ),
                (
                    "만드셨으면 이리로 돌아오세요",
                    '<p><a class="btn btn-primary" href="/auth/repo/done">'
                    "만들었어요, 다음 →</a></p>"
                    "<p>다음 화면에서 방금 만든 저장소에 접근을 허용해 주시면 "
                    "됩니다.</p>",
                ),
            ]
        )
        + "</div>"
        + '<div class="card card-soft">'
        '<h2 style="margin-top:0">이미 쓰던 저장소가 있어요</h2>'
        "<p>그 저장소를 그대로 쓰셔도 됩니다.</p>"
        f'<p style="margin-bottom:0"><a class="btn" href="{_INSTALL_PATH}">'
        "있는 저장소 고르기</a></p></div>",
    )


# ---------------------------------------------------------------------------
# 내 페이지(namu-60) — 로그인 왕복이 끝난 "그 순간"에만 볼 수 있던 접속 주소를,
# 창을 닫은 뒤에도 다시 볼 수 있는 영구 경로. 세 가지 이유로 상태를 엄격히
# 나눈다:
#   1) 세션이 없거나(로그인 안 함) 위조/만료됐으면 본인 정보를 한 글자도
#      보여주지 않는다 — user_key/저장소명/접속 주소는 전부 그 사람 전용이라,
#      "세션이 있는 척"만 해도 노출되면 인증을 건너뛴 것과 같다.
#   2) 서명은 유효한데 장부에 그 사용자가 없으면(예: 장부 재구축) 500으로
#      터뜨리지 않고 로그인 안내와 동일하게 취급한다.
#   3) 저장소 미연결/열쇠 미발급은 각각 "설치 유도"/"그 자리에서 발급"으로
#      풀어야 빈 화면이 나오지 않는다.
# ---------------------------------------------------------------------------
def _html_me_login_required() -> str:
    body = (
        "<h1>로그인이 필요합니다</h1>"
        '<p class="lead">로그인 상태가 없거나 시간이 지나 풀렸습니다. '
        "다시 로그인해 주세요.</p>"
        '<div class="btn-row">'
        '<a class="btn btn-primary" href="/auth/github/login">GitHub으로 로그인</a>'
        '<a class="btn" href="/">홈으로</a>'
        "</div>"
    )
    return _html_page("NAMU 로그인 필요", body, cta="start")


def _html_me_not_connected(user_key: str, notice_html: str = "") -> str:
    install_url = "/auth/github/install"
    body = (
        '<span class="eyebrow">내 페이지</span>'
        "<h1>아직 연결된 저장소가 없습니다</h1>"
        f"{notice_html}"
        '<p class="lead">기억을 담을 저장소를 하나 정해 주셔야 접속 주소가 '
        "만들어집니다. 저장소가 없으시면 만드는 것부터 함께 안내합니다.</p>"
        '<div class="btn-row">'
        f'<a class="btn btn-primary" href="{html.escape(install_url)}">'
        "저장소 연결하기</a>"
        '<a class="btn" href="/start">먼저 절차 보기</a>'
        "</div>"
        "<hr>"
        f"<p><small>사용자 키: <code>{html.escape(user_key)}</code> · "
        '<a href="/auth/logout">로그아웃</a></small></p>'
    )
    return _html_page("NAMU 내 페이지", body)


# 주소 관리 버튼들. 전부 **POST 폼**이다 — 링크(GET)로 두면 브라우저 프리페치나
# 채팅에 붙여넣은 미리보기 크롤러가 클릭 없이 열쇠를 갈아버릴 수 있다.
# CSRF는 세션 쿠키의 SameSite=Lax가 막는다(다른 사이트에서 보낸 POST에는 이
# 쿠키가 실리지 않아 세션 없음으로 거절된다 — `_session_user_key` 참고).
# 연결 시험만 화면을 새로 띄우지 않고 그 자리에서 처리한다(namu-69).
#
# 왜 이 버튼만 다른가: 재발급·폐기는 화면 내용 자체가 바뀌므로(새 주소가 나오거나
# 주소가 사라진다) 페이지가 다시 그려지는 것이 곧 결과다. 반면 연결 시험은 아무것도
# 바꾸지 않아서, 다시 그려도 화면이 전과 똑같아 보인다 — 실측에서 사용자가 "새로고침만
# 된 것 같고 응답이 온 건지 알 수 없었다"고 보고한 지점이 정확히 여기다. 게다가 프로브는
# 최대 십여 초가 걸려 그동안 화면이 비어 있다.
#
# 그래서 ①누른 즉시 "확인하는 중"을 그 자리에 띄우고 ②결과를 같은 자리에서 알림
# 상자로 바꿔 넣는다. 화면 전환이 없으니 **달라진 것은 그 상자 하나뿐**이라 눈에 띈다.
#
# 자바스크립트가 없거나 실패해도 폼은 그대로 남아 있어 종전처럼 전체 페이지가 다시
# 그려지며 같은 결과가 나온다(기능이 사라지지 않는다).
_MCP_TEST_PROGRESS_HTML = ui.notice(
    "<b>확인하는 중입니다.</b> 최대 {wait}초쯤 걸릴 수 있습니다 — 이 화면을 "
    "그대로 두고 잠시만 기다려 주세요.",
    tone="wait",
    attrs='id="mcp-test-progress"',
    # 스크립트가 `p.style.display`로 켜고 끈다 — 그래서 감추는 일만 인라인이다.
    style_extra="display:none;",
)

_MCP_TEST_FAILED_HTML = (
    "<b>확인 요청을 보내지 못했습니다.</b> 인터넷 연결이 끊겼거나 로그인이 만료됐을 "
    "수 있습니다 — 화면을 새로고침한 뒤 다시 눌러 보세요."
)

def _html_mcp_test_script() -> str:
    """연결 시험 버튼을 그 자리에서 처리하는 스크립트(외부 스크립트 없음).

    함수인 이유: 실패 안내 문구를 `_html_notice`로 만들어 심는데, 그 함수가 이
    파일 뒤쪽에 정의돼 있어 모듈 상수로 두면 로드 시점에 아직 없다. 화면 조각을
    만드는 다른 함수들과 같은 원칙(그릴 때 만든다)이기도 하다.
    """
    failed = json.dumps(_html_notice(_MCP_TEST_FAILED_HTML, tone="warn"))
    return (
        "<script>"
        "(function(){"
        "var f=document.getElementById('mcp-test-form');"
        # 옛 브라우저(fetch 없음)는 손대지 않는다 — 폼 제출로 종전처럼 동작한다.
        "if(!f||!window.fetch)return;"
        "var b=document.getElementById('mcp-test-btn');"
        "var p=document.getElementById('mcp-test-progress');"
        "var o=document.getElementById('mcp-test-result');"
        "var label=b.textContent;"
        "var show=function(html){o.innerHTML=html;"
        "var box=o.firstElementChild;"
        "if(box){box.className='namu-pop';"
        "if(box.scrollIntoView){box.scrollIntoView({block:'nearest'});}}};"
        "f.addEventListener('submit',function(e){"
        "e.preventDefault();"
        "b.disabled=true;b.textContent='확인 중…';"
        "o.innerHTML='';p.style.display='block';"
        "fetch(f.action,{method:'POST',credentials:'same-origin',"
        "headers:{'Accept':'application/json'}})"
        ".then(function(r){return r.json();})"
        ".then(function(d){show(d.notice_html||'');})"
        f".catch(function(){{show({failed});}})"
        ".then(function(){p.style.display='none';b.disabled=false;"
        "b.textContent=label;});"
        "});"
        "})();"
        "</script>"
    )


def _html_mcp_actions() -> str:
    """주소 관리 3버튼 + 연결 시험의 진행/결과 자리.

    대기 시간 안내는 프로브 상수에서 계산한다 — 손으로 적은 숫자는 타임아웃을 조정한
    순간 조용히 거짓말이 된다(그 문구를 믿고 기다리는 사람에게는 그게 곧 고장이다).
    """
    wait = math.ceil(_MCP_PROBE_TIMEOUT_SEC * 2 + _MCP_PROBE_RETRY_DELAY_SEC)
    return (
        '<div class="card">'
        '<h2 style="margin-top:0">주소 관리</h2>'
        '<div class="btn-row">'
        '<form method="post" action="/auth/mcp/test" id="mcp-test-form">'
        '<button type="submit" id="mcp-test-btn" class="btn">'
        "연결 시험</button></form>"
        '<form method="post" action="/auth/mcp/rotate">'
        '<button type="submit" class="btn">주소 재발급</button></form>'
        '<form method="post" action="/auth/mcp/revoke">'
        '<button type="submit" class="btn btn-danger">주소 폐기</button></form>'
        "</div>"
        + _MCP_TEST_PROGRESS_HTML.format(wait=wait)
        + '<div id="mcp-test-result"></div>'
        + '<p style="margin-bottom:0"><small><b>연결 시험</b>은 지금 이 주소가 '
        "실제로 응답하는지 확인합니다. <b>재발급</b>은 새 주소를 만들고 옛 주소를 "
        "즉시 막습니다(AI에 등록해 둔 커넥터 주소도 새로 바꿔 주셔야 합니다). "
        "<b>폐기</b>는 주소를 아예 없앱니다 — 누르면 한 번 더 확인합니다."
        "</small></p></div>"
        + _html_mcp_test_script()
    )


def _html_me_connected(
    user_key: str,
    repo_full_name: str,
    mcp_url: "str | None",
    *,
    notice_html: str = "",
    revoked: bool = False,
) -> str:
    body = [
        '<span class="eyebrow">내 페이지</span>',
        "<h1>내 기억과 접속 주소</h1>",
        notice_html,
        f'<p class="lead">연결된 저장소: '
        f"<code>{html.escape(repo_full_name)}</code></p>",
        '<div class="card card-accent">'
        '<h2 style="margin-top:0">내 기억</h2>'
        "<p>AI가 무엇을 기억했는지 사람 눈으로 확인하고 찾아볼 수 있습니다.</p>"
        '<div class="btn-row" style="margin-bottom:0">'
        '<a class="btn btn-primary" href="/auth/memory">기억 열람·검색</a>'
        '<a class="btn" href="/auth/memory?bowl=tasks">열린 작업 보기</a>'
        "</div></div>",
    ]
    if mcp_url:
        body.append(_html_onboarding_section(mcp_url))
        body.append(_html_mcp_actions())
    elif revoked:
        # 사용자가 스스로 없앤 상태 — "만들지 못했습니다"(장애)와 절대 같은
        # 문구를 쓰면 안 된다. 되돌리는 방법(재발급)을 그 자리에 둔다.
        body.append(
            '<div class="card">'
            '<h2 style="margin-top:0">접속 주소</h2>'
            "<p><b>접속 주소를 폐기하셨습니다.</b> 지금은 어떤 AI도 회원님 기억에 "
            "접속할 수 없습니다. 다시 쓰시려면 아래에서 새 주소를 발급받으세요 "
            "— 저장소 연결은 그대로 남아 있으니 처음부터 다시 하실 필요는 "
            "없습니다.</p>"
            '<form method="post" action="/auth/mcp/rotate">'
            '<button type="submit" class="btn btn-primary">새 주소 발급</button>'
            "</form></div>"
        )
    else:
        # 여기 도달하면 호출부가 열쇠 발급을 이미 시도했어야 정상이다 — 그래도
        # 실패했다면(예: 장부 쓰기 실패) 빈 화면 대신 이유를 알린다.
        body.append(
            _html_notice(
                "<b>접속 주소를 만들지 못했습니다.</b> 잠시 후 새로고침해도 안 "
                "되면 관리자에게 알려주세요.",
                tone="bad",
            )
        )
    body.append(
        "<hr>"
        f"<p><small>사용자 키: <code>{html.escape(user_key)}</code> · "
        '<a href="/auth/logout">로그아웃</a></small></p>'
    )
    return _html_page("NAMU 내 페이지", "".join(body))


def _html_rotate_confirm() -> str:
    """재발급 확인 화면 — 폐기와 같은 문턱을 둔다.

    왜 필요한가: 재발급은 "새 주소가 생긴다"로 들려서 무해해 보이지만, 실제
    결과는 **옛 주소가 그 자리에서 막히는 것**이다. AI에 등록해 둔 커넥터는
    그 순간부터 기억에 닿지 못하고, 사용자는 등록 절차를 다시 밟아야 한다.
    폐기는 한 번 더 묻는데 재발급은 안 묻는 것은, 결과의 무게가 비슷한데
    문턱만 다른 상태였다(사용자 지적, 2026-08-02).

    확인을 자바스크립트 알림창이 아니라 화면으로 받는 이유: 폐기가 이미 그
    방식이라 같은 결로 맞추고, 스크립트가 막힌 브라우저에서도 똑같이 동작한다.
    """
    body = (
        "<h1>주소를 새로 발급할까요?</h1>"
        '<div class="card">'
        "<p>새 주소를 만들면 <b>지금 쓰는 주소는 그 즉시 막힙니다.</b> "
        "AI에 등록해 둔 커넥터도 그 순간부터 회원님 기억에 닿지 못합니다.</p>"
        '<p style="margin-bottom:0">그래서 발급 후에는 <b>AI의 커넥터 주소를 '
        "새것으로 바꿔 주셔야</b> 합니다 — 그 일까지 하실 준비가 되셨을 때 "
        "진행하세요.</p>"
        "</div>"
        + _html_notice(
            "주소가 새어 나갔거나 남이 알게 된 경우가 아니라면, 굳이 바꾸실 "
            "이유는 없습니다.",
            tone="info",
        )
        + '<div class="btn-row">'
        '<form method="post" action="/auth/mcp/rotate">'
        '<input type="hidden" name="confirm" value="yes">'
        '<button type="submit" class="btn btn-primary">네, 새로 발급합니다</button>'
        "</form>"
        '<a class="btn" href="/auth/me">아니요, 돌아가기</a>'
        "</div>"
    )
    return _html_page("NAMU 주소 재발급 확인", body)


def _html_revoke_confirm() -> str:
    """폐기 확인 화면 — 실수로 한 번 누른 것만으로는 주소가 사라지지 않게 하는
    단계. 이 화면 자체는 아무것도 바꾸지 않는다(확인 폼을 다시 POST해야 실행)."""
    body = (
        "<h1>정말 주소를 폐기할까요?</h1>"
        '<div class="card">'
        "<p>폐기하면 지금 쓰고 있는 접속 주소가 <b>즉시 막힙니다.</b> AI에 등록해 "
        "둔 커넥터도 그 순간부터 회원님 기억에 닿지 못합니다.</p>"
        '<p style="margin-bottom:0">기억 자체는 회원님 GitHub 저장소에 그대로 '
        "남습니다 — 나중에 새 주소를 발급받으면 다시 이어서 쓰실 수 있습니다.</p>"
        "</div>"
        '<div class="btn-row">'
        '<form method="post" action="/auth/mcp/revoke">'
        '<input type="hidden" name="confirm" value="yes">'
        '<button type="submit" class="btn btn-danger">네, 폐기합니다</button></form>'
        '<a class="btn" href="/auth/me">아니요, 돌아가기 (Cancel)</a>'
        "</div>"
    )
    return _html_page("NAMU 주소 폐기 확인", body)


def _html_logged_out() -> str:
    body = (
        "<h1>로그아웃했습니다</h1>"
        '<p class="lead">기억은 회원님 저장소에 그대로 있습니다 — 다시 '
        "로그인하시면 이어서 쓰실 수 있습니다.</p>"
        '<div class="btn-row">'
        '<a class="btn btn-primary" href="/auth/github/login">다시 로그인</a>'
        '<a class="btn" href="/">홈으로</a>'
        "</div>"
    )
    return _html_page("NAMU 로그아웃", body, cta="start")


# ---------------------------------------------------------------------------
# 연결 시험(namu-60) — "지금 이 주소가 실제로 동작하는가"를 서버가 대신 확인해
# 준다.
#
# ## 왜 자기 자신을 127.0.0.1로 부르는가
#
# 바깥 도메인(namu-cloud.onnamu.kr)으로 부르면 요청이 Cloudflare 터널을 한
# 바퀴 돌아 다시 들어와야 하고, 컨테이너 안에서는 그 이름이 아예 풀리지 않을
# 수 있다(내부 DNS에 없다) — 주소는 멀쩡한데 "확인 불가"만 나오는 시험이 된다.
# 우리가 확인하려는 것은 "이 열쇠가 우리 서버에서 유효한가"이지 "터널이
# 살아있는가"가 아니므로, 이 프로세스가 바인드한 포트로 직접 두드린다.
#
# ## 판정은 반드시 세 갈래다
#
#   살아있음 / 주소가 잘못됨 / **지금은 확인 불가**
#
# 세 번째가 이 기능의 핵심이다. 배포 직후 컨테이너가 아직 안 떠서 나는 연결
# 거부, 재시작 중의 일시 502, 느린 응답을 "죽었다"로 단정하면, 멀쩡한 주소를
# 쥔 사용자가 재발급 버튼을 눌러 스스로 커넥터를 깨뜨린다. 그래서 404(장부에
# 없는 열쇠 = 문지기가 명시적으로 거절한 경우)만 "잘못됨"이고, 5xx·타임아웃·
# 연결 실패는 1초 뒤 한 번 더 두드려 보고 그래도 같으면 "확인 불가"다.
# ---------------------------------------------------------------------------
# 프로브 자체는 짧게 끊는다 — 이 시간 동안 사용자는 버튼을 누른 채 기다린다.
_MCP_PROBE_TIMEOUT_SEC = 5.0
_MCP_PROBE_RETRY_DELAY_SEC = 1.0

_PROBE_ALIVE = "alive"
_PROBE_INVALID = "invalid"
_PROBE_UNKNOWN = "unknown"

# 이 서버가 바인드하는 포트. 새 환경변수를 만들지 않고 routing_server.main()이
# 읽는 것과 **같은** 변수를 읽는다(배포는 Dockerfile의 ENV NAMU_HTTP_PORT=8770로
# 항상 채워져 있다). 미설정 시 기본값도 routing_server.main()과 같아야 하며,
# 어긋나면 tests/test_web_auth.py의 드리프트 테스트가 실패한다.
_DEFAULT_SELF_PORT = 8770


def _self_http_port() -> int:
    raw = os.environ.get("NAMU_HTTP_PORT", "").strip()
    if not raw:
        return _DEFAULT_SELF_PORT
    try:
        return int(raw)
    except ValueError:
        # 기동 자체가 이 값으로 이뤄지므로 여기까지 왔다면 실사용에서는 나올 수
        # 없는 값이다 — 시험 하나 때문에 화면을 500으로 터뜨리지 않고 기본값으로
        # 물러난다(판정은 어차피 "확인 불가"로 안전하게 끝난다).
        logger.warning("NAMU_HTTP_PORT 값이 정수가 아닙니다 — 연결 시험은 기본 포트로 시도합니다")
        return _DEFAULT_SELF_PORT


def _self_mcp_probe_url(mcp_secret: str) -> str:
    return f"http://127.0.0.1:{_self_http_port()}/mcp/{mcp_secret}"


def _http_probe(url: str) -> "int | None":
    """자기 자신에게 MCP `initialize`를 한 번 보내고 상태코드만 돌려준다.

    `initialize`를 고른 이유: MCP 규약상 세션을 여는 첫 인사이고 **부작용이
    없다** — 도구를 부르지 않으므로 사용자 저장소를 건드리지 않는다(이 서버는
    stateless HTTP라 세션이 남지도 않는다).

    응답 자체를 못 받으면(연결 거부·타임아웃) None. 예외를 밖으로 내보내지
    않는 이유는 호출부가 "확인 불가"로 처리해야 하기 때문이다.

    테스트가 걷어낼 유일한 네트워크 접점이다(`_http_json`과 같은 원칙).
    """
    import httpx

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "namu-cloud-self-check", "version": "1"},
        },
    }
    try:
        resp = httpx.post(
            url,
            json=body,
            headers={
                # streamable HTTP는 두 형식을 모두 받을 수 있다고 알려야 한다 —
                # 하나만 적으면 서버가 406으로 거절한다.
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            timeout=_MCP_PROBE_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001 — 네트워크 계열 전부를 "확인 불가"로 모은다
        logger.info("연결 시험: 응답을 받지 못했습니다 (%s)", type(exc).__name__)
        return None
    return resp.status_code


def probe_mcp_connection(mcp_secret: str) -> str:
    """`_PROBE_ALIVE` / `_PROBE_INVALID` / `_PROBE_UNKNOWN` 중 하나.

    **블로킹 함수다** — 반드시 스레드풀에서 부를 것(호출부 `mcp_test` 참고).
    이벤트 루프 위에서 그대로 부르면 서버가 자기 자신에게 보낸 요청을 처리할
    수 없어 타임아웃까지 굳는다(자기 호출 특유의 교착).
    """
    url = _self_mcp_probe_url(mcp_secret)
    status = _http_probe(url)
    if status is None or status >= 500:
        # 일시적 실패일 수 있다 — 짧게 한 번만 다시 본다. 여기서 여러 번
        # 재시도하면 사용자가 빈 화면을 오래 보게 된다.
        time.sleep(_MCP_PROBE_RETRY_DELAY_SEC)
        status = _http_probe(url)

    if status == 404:
        # 문지기(_PerUserSecretDispatcher)가 장부에서 열쇠를 못 찾았다는 뜻 —
        # 폐기됐거나 이미 재발급돼 죽은 주소다. 이것만이 "잘못됨" 판정이다.
        return _PROBE_INVALID
    if status is not None and 200 <= status < 300:
        return _PROBE_ALIVE
    logger.info("연결 시험: 판정 보류 (status=%s)", status)
    return _PROBE_UNKNOWN


# 알림 상자의 색·아이콘. 색만으로는 구분되지 않는다 — 실측(namu-69)에서 사용자가
# "페이지가 새로고침된 것처럼만 보이고 뭐가 달라졌는지 눈에 안 띈다"고 보고했다.
# 그래서 ①맨 앞에 뜻이 바로 읽히는 아이콘을 두고 ②옅은 배경색을 깔아 상자 자체가
# 본문과 분리돼 보이게 한다. 배경은 반투명(rgba)이라 라이트·다크 어느 쪽에서도
# 글자 대비를 해치지 않는다(색상값을 테마별로 두 벌 관리하지 않아도 된다).
def _html_notice(text_html: str, *, tone: str = "info") -> str:
    """결과 알림 상자 — 실체는 `ui.notice`다.

    이 조각만은 클래스가 아니라 인라인 스타일을 지고 다닌다. 연결 시험(namu-69)이
    이 조각을 **JSON으로 실어 보내** 자바스크립트가 화면에 심기 때문이다 —
    자세한 이유는 `ui.py` 첫머리에 적어 두었다.
    """
    return ui.notice(text_html, tone=tone)


_PROBE_NOTICES = {
    _PROBE_ALIVE: _html_notice(
        "<b>살아있습니다.</b> 지금 이 주소는 정상적으로 응답합니다 — 그대로 쓰시면 "
        "됩니다.",
        tone="good",
    ),
    _PROBE_INVALID: _html_notice(
        "<b>주소가 잘못됐습니다.</b> 이 주소는 폐기됐거나 더 이상 유효하지 "
        "않습니다. 아래에서 주소를 재발급받고, AI에 등록해 둔 커넥터 주소도 새 "
        "것으로 바꿔 주세요.",
        tone="bad",
    ),
    _PROBE_UNKNOWN: _html_notice(
        "<b>지금은 확인할 수 없습니다.</b> 서버가 응답하지 않거나 일시적인 오류가 "
        "났습니다 — <b>주소가 잘못됐다는 뜻은 아닙니다.</b> 방금 서버를 새로 "
        "올린 직후라면 잠깐 그럴 수 있으니, 1~2분 뒤에 다시 눌러 보세요.",
        tone="warn",
    ),
}

_NOTICE_ROTATED = _html_notice(
    "<b>새 주소를 발급했습니다.</b> 옛 주소는 지금 이 순간부터 막혔습니다 — AI에 "
    "등록해 둔 커넥터 주소를 아래 새 주소로 바꿔 주세요.",
    tone="good",
)

_NOTICE_REVOKED = _html_notice(
    "<b>주소를 폐기했습니다.</b> 옛 주소로는 더 이상 접속할 수 없습니다.",
    tone="warn",
)

# 화면 안에서 결과만 받아 가는 요청이 세션 만료로 거절될 때 쓰는 알림(namu-69).
# 전체 페이지를 다시 그리는 경로는 종전대로 로그인 안내 화면으로 보낸다 — 그쪽은
# 화면 전체가 바뀌므로 안내가 묻히지 않지만, 그 자리에 심는 요청은 이 한 줄이
# 없으면 아무 일도 일어나지 않은 것처럼 보인다.
_NOTICE_LOGIN_EXPIRED = _html_notice(
    "<b>로그인이 만료됐습니다.</b> 화면을 새로고침해 다시 로그인한 뒤 눌러 주세요.",
    tone="warn",
)

_NOTICE_NO_SECRET_TO_TEST = _html_notice(
    "<b>시험할 주소가 없습니다.</b> 접속 주소를 폐기하신 상태입니다 — 새 주소를 "
    "발급받은 뒤 다시 시험해 주세요.",
    tone="warn",
)


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
async def login(request: Request) -> Response:
    state = secrets.token_urlsafe(24)
    query = urlencode({"client_id": ga.client_id(), "state": state})
    resp = RedirectResponse(url=f"{GITHUB_AUTHORIZE_URL}?{query}", status_code=302)
    # redirect_uri는 의도적으로 싣지 않는다 — GitHub App에 등록된 Callback URL
    # (https://namu-cloud.onnamu.kr/auth/github/callback) 하나에만 의존한다.
    # 설정 항목을 하나 줄이는 것과 동시에, "코드의 redirect_uri와 앱 등록값이
    # 어긋나 GitHub이 거부하는" 흔한 버그 클래스 자체를 제거하기 위한 의도적
    # 선택이다.
    resp.set_cookie(
        _STATE_COOKIE_NAME,
        _sign_with_expiry(state, _STATE_COOKIE_TTL_SEC),
        max_age=_STATE_COOKIE_TTL_SEC,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/auth",
    )
    return resp


async def install(request: Request) -> Response:
    """앱 설치 화면으로 보내되, login과 똑같이 state 도장을 찍어 보낸다.

    설치를 마치면 GitHub은 등록된 Callback URL로 `code`·`installation_id`·
    `setup_action=install`을 실어 돌려보낸다. 이때 state는 **설치를 시작한
    URL에 실린 것만** 되돌아오므로, 안내 화면에서 GitHub 설치 주소를 직접
    링크하면 state 없이 돌아와 callback이 400으로 거절한다(2026-07-26 실측).
    쿠키는 링크가 아니라 응답만 심을 수 있어, 이 경로를 한 번 경유시키는 것이
    설치 왕복에 도장을 붙이는 유일한 방법이다.

    쿠키 설정은 login과 동일해야 한다 — 한쪽만 바꾸면 왕복이 조용히 깨진다.
    """
    state = secrets.token_urlsafe(24)
    query = urlencode({"state": state})
    url = f"{GITHUB_INSTALL_URL_TMPL.format(slug=_app_slug())}?{query}"
    resp = RedirectResponse(url=url, status_code=302)
    resp.set_cookie(
        _STATE_COOKIE_NAME,
        _sign_with_expiry(state, _STATE_COOKIE_TTL_SEC),
        max_age=_STATE_COOKIE_TTL_SEC,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/auth",
    )
    return resp


async def callback(request: Request) -> Response:
    query_state = request.query_params.get("state") or ""
    cookie_state = _unsign_with_expiry(request.cookies.get(_STATE_COOKIE_NAME))
    state_ok = bool(query_state) and bool(cookie_state) and hmac.compare_digest(cookie_state, query_state)
    if not state_ok:
        resp = PlainTextResponse(
            "로그인 요청을 검증하지 못했습니다(state 불일치/만료) — 처음부터 다시 "
            "로그인해 주세요. Could not verify the login request (state mismatch).",
            status_code=400,
        )
        resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
        return resp
    # state 검증 통과 — 재사용 방지를 위해 여기서 쿠키를 지운다(아래 모든 반환
    # 경로가 delete_cookie를 다시 호출하는 이유).

    code = request.query_params.get("code") or ""
    if not code:
        resp = PlainTextResponse("code 파라미터가 없습니다. Missing code.", status_code=400)
        resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
        return resp

    try:
        # user_token은 이 요청을 처리하는 동안만 쓰는 지역 변수다 — 응답을 만든
        # 뒤에는 어디에도 저장하지 않고 버려진다(설계 원칙: 영구 열쇠를 보관하지
        # 않는다).
        user_token = _exchange_code_for_token(code)
        user_info = _fetch_github_user(user_token)
    except (ValueError, RuntimeError) as exc:
        resp = PlainTextResponse(str(exc), status_code=400)
        resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
        return resp

    github_id = user_info.get("id")
    login_name = user_info.get("login")

    try:
        with closing(identity.connect()) as conn:
            user_key = identity.upsert_user(conn, github_id, login_name)

            # 이미 저장소가 연결된 사용자(installation_id/repo_full_name이 이전
            # 로그인에서 이미 채워짐)라면 매번 GitHub 설치/저장소 목록을 다시
            # 조회해 고르라고 하지 않는다 — 내 페이지로 곧장 보낸다. 세션
            # 쿠키는 반드시 이 리다이렉트 응답 자체에 심는다(내 페이지가 쿠키
            # 없이는 아무것도 보여주지 못하므로).
            already_connected = identity.get_by_user_key(conn, user_key)
            if already_connected and already_connected.get("installation_id") and \
                    already_connected.get("repo_full_name"):
                logger.info("GitHub 로그인 완료(기존 연결 유지, user_key=%s)", user_key)
                resp = RedirectResponse(url="/auth/me", status_code=302)
                resp.set_cookie(
                    _SESSION_COOKIE_NAME,
                    _sign_with_expiry(user_key, _SESSION_COOKIE_TTL_SEC),
                    max_age=_SESSION_COOKIE_TTL_SEC,
                    httponly=True,
                    secure=True,
                    samesite="lax",
                    path="/auth",
                )
                resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
                return resp

            installation_id_raw = request.query_params.get("installation_id") or ""
            installs_truncated = False
            if installation_id_raw:
                try:
                    installation_id = int(installation_id_raw)
                except ValueError:
                    resp = PlainTextResponse(
                        "installation_id가 올바르지 않습니다. Invalid installation_id.",
                        status_code=400,
                    )
                    resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
                    return resp
                installation_ids = [installation_id]
            else:
                # GitHub은 **설치가 새로 일어난** 왕복에만 installation_id를 실어
                # 준다. 이미 설치한 사용자는 설치 링크가 설정 화면으로 넘어가고
                # 바꿀 것이 없으면 Save가 비활성이라 되돌아오는 왕복 자체가 없어,
                # 저장소 연결을 영영 끝낼 수 없었다(2026-07-26 실측). 그래서 여기서
                # 사용자 토큰으로 기존 설치를 직접 조회한다.
                installation_ids, installs_truncated = _fetch_user_installations(user_token)

            # 저장소를 아직 하나도 허용하지 않은 사람 — 2단계 화면으로 보낸다.
            # 화면을 여기서 그려 주지 않고 주소를 넘기는 이유: 그 화면은 새 탭에
            # 다녀와서 **다시 돌아올** 자리라 자기 주소가 있어야 한다(그리고
            # 새로고침해도 살아 있어야 한다).
            if not installation_ids:
                logger.info("GitHub 로그인 완료(저장소 미연결, user_key=%s)", user_key)
                resp = RedirectResponse(url="/auth/repo", status_code=302)
                resp.set_cookie(
                    _SESSION_COOKIE_NAME,
                    _sign_with_expiry(user_key, _SESSION_COOKIE_TTL_SEC),
                    max_age=_SESSION_COOKIE_TTL_SEC,
                    httponly=True,
                    secure=True,
                    samesite="lax",
                    path="/auth",
                )
                resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
                return resp

            hint = _repo_hint(request)
            if len(installation_ids) == 1:
                installation_id = installation_ids[0]
                repos, truncated = _fetch_installation_repos(user_token, installation_id)
                hinted = _pick_hinted_repo(hint, repos)
                if hinted:
                    # 방금 만들어 온 저장소가 목록에 있다 — "어느 거였죠?"라고
                    # 되묻지 않는다. 목록이 잘렸는지는 상관없다(찾던 것을 찾았다).
                    identity.set_installation(conn, user_key, installation_id, hinted)
                    body_html = _html_connected(
                        user_key, hinted, _mcp_url_for(request, conn, user_key)
                    )
                elif truncated:
                    # 목록이 잘렸을 수 있으므로 몇 개가 잡혔든 자동 연결(1개일 때
                    # 곧장 연결하는 경로)을 타지 않는다 — "이게 정말 유일한
                    # 저장소"라는 확신이 없기 때문이다. 사용자가 직접 고르게 하고
                    # 화면에 잘렸다는 사실을 알린다.
                    body_html = _html_select_repo(
                        user_key, installation_id, repos, truncated=True
                    )
                elif len(repos) == 1:
                    identity.set_installation(conn, user_key, installation_id, repos[0])
                    body_html = _html_connected(
                        user_key, repos[0], _mcp_url_for(request, conn, user_key)
                    )
                elif len(repos) == 0:
                    body_html = _html_no_repos(installation_id)
                else:
                    body_html = _html_select_repo(user_key, installation_id, repos)
            else:
                # 설치가 둘 이상(개인 계정 + 조직 등) — 저장소 이름만으로는 어느
                # 설치를 거쳐야 하는지 알 수 없으므로 쌍으로 모아 한 화면에 낸다.
                pairs: "list[tuple[int, str]]" = []
                truncated = installs_truncated
                for iid in installation_ids:
                    repos, repos_truncated = _fetch_installation_repos(user_token, iid)
                    truncated = truncated or repos_truncated
                    pairs.extend((iid, repo) for repo in repos)
                hinted_pairs = [
                    (iid, repo)
                    for iid, repo in pairs
                    if _pick_hinted_repo(hint, [repo])
                ]
                if not pairs:
                    body_html = _html_no_repos(installation_ids[0])
                elif len(hinted_pairs) == 1:
                    # 방금 만들어 온 저장소가 어느 설치에 붙었는지까지 하나로
                    # 정해진 경우에만 건너뛴다 — 개인 계정과 조직에 같은 이름이
                    # 둘 다 있으면 지어내지 않고 고르게 한다.
                    hint_iid, hint_repo = hinted_pairs[0]
                    identity.set_installation(conn, user_key, hint_iid, hint_repo)
                    body_html = _html_connected(
                        user_key, hint_repo, _mcp_url_for(request, conn, user_key)
                    )
                elif len(pairs) == 1 and not truncated:
                    # 목록이 잘리지 않은 상태에서 후보가 하나뿐이면 고를 것이
                    # 없다 — 설치가 1개일 때와 같은 기준으로 곧장 연결한다.
                    only_iid, only_repo = pairs[0]
                    identity.set_installation(conn, user_key, only_iid, only_repo)
                    body_html = _html_connected(
                        user_key, only_repo, _mcp_url_for(request, conn, user_key)
                    )
                else:
                    body_html = _html_select_repo_multi(user_key, pairs, truncated=truncated)
    except (ValueError, RuntimeError) as exc:
        resp = PlainTextResponse(str(exc), status_code=400)
        resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
        return resp

    logger.info("GitHub 로그인 완료 (user_key=%s)", user_key)

    resp = HTMLResponse(body_html)
    resp.set_cookie(
        _SESSION_COOKIE_NAME,
        _sign_with_expiry(user_key, _SESSION_COOKIE_TTL_SEC),
        max_age=_SESSION_COOKIE_TTL_SEC,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/auth",
    )
    resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
    # "만들었어요" 표시는 이 왕복에서 쓰고 버린다 — 남겨 두면 나중에 저장소를
    # 바꾸려고 다시 온 사람이 옛 이름으로 조용히 연결되는 사고가 난다.
    resp.delete_cookie(_REPO_HINT_COOKIE_NAME, path="/auth")
    return resp


def _session_user_key(request: Request) -> "str | None":
    """세션 쿠키에서 검증된 user_key를 꺼낸다. 없거나 위조·만료면 None.

    로그인 이후의 모든 화면·동작(`select_repo`/`me`/주소 관리 POST 3종)이
    **이 함수 하나**를 통과한다 — 신뢰 경로를 새로 만들지 않기 위한 단일 지점이다.
    """
    return _unsign_with_expiry(request.cookies.get(_SESSION_COOKIE_NAME))


# ---------------------------------------------------------------------------
# 공개 페이지(namu-70) — 로그인 없이 누구나 보는 화면. 내용은 pages.py에 있고
# 여기서는 **세션이 있는지만 알려 준다.**
#
# 왜 이 앱에 얹나: 요청을 가르는 디스패처(routing_server)가 "인증 있는 쪽"을
# 기본값으로 두기 때문에, 공개 경로는 이 웹 앱으로 명시적으로 보내는 수밖에 없다.
# 그렇다고 pages.py가 쿠키를 읽게 하면 공개 화면이 인증 코드를 품게 되므로,
# 판정은 여기서 하고 결과(True/False)만 넘긴다.
#
# 세션이 위조·만료됐어도 공개 페이지는 그냥 "로그인 안 한 사람"으로 그린다 —
# 이 화면들은 회원 정보를 한 글자도 싣지 않으므로 거절할 이유가 없다.
# ---------------------------------------------------------------------------
def _public_page(path: str):
    render = pages.PAGES[path]

    async def handler(request: Request) -> Response:
        logged_in = bool(_session_user_key(request))
        return HTMLResponse(render(logged_in))

    handler.__name__ = f"public_page_{path.strip('/') or 'home'}"
    return handler


# ---------------------------------------------------------------------------
# 2단계 화면과 "만들었어요, 다음" (namu-70)
#
# ## 왜 저장소 이름을 쿠키에 실어 보내나
#
# [만들었어요, 다음]을 누른 사람은 방금 `namu-memory`를 만들고 온 사람이다.
# 그런데 그 다음 왕복(GitHub 설치 승인)이 끝나 돌아왔을 때, 우리는 그 사실을
# 기억할 방법이 없어 **저장소가 여러 개면 다시 고르라고** 묻게 된다 — 방금
# 만들어 온 사람에게 "어느 거였죠?"라고 되묻는 셈이다. 그래서 그 이름을 서명
# 쿠키에 담아 두었다가, 돌아온 목록에 그 이름이 있으면 고르기 화면을 건너뛴다.
#
# 이 쿠키는 **권한을 주지 않는다.** 고를 수 있는 것은 어차피 GitHub이 그 설치에
# 허용한 저장소 목록 안뿐이라, 이름을 위조해도 남의 저장소에 닿지 못한다.
# 그래도 서명하는 이유는 값이 손대졌는지 서버가 알아야 조용한 오작동을 피하기
# 때문이다(이 파일의 다른 쿠키와 같은 규약).
#
# 이름을 바꿔 만든 사람도 막히지 않는다 — 목록에 그 이름이 없으면 아무 일도
# 일어나지 않고 평소대로 고르기 화면이 나온다.
# ---------------------------------------------------------------------------
_REPO_HINT_COOKIE_NAME = "namu_repo_hint"
_REPO_HINT_TTL_SEC = 1800
_DEFAULT_NEW_REPO_NAME = "namu-memory"


def _repo_hint(request: Request) -> "str | None":
    """[만들었어요, 다음]을 눌렀을 때 담아 둔 저장소 이름. 없거나 위조면 None."""
    return _unsign_with_expiry(request.cookies.get(_REPO_HINT_COOKIE_NAME))


def _pick_hinted_repo(hint: "str | None", repos: "list[str]") -> "str | None":
    """돌아온 목록에서 그 이름의 저장소를 찾는다. 없으면 None(평소대로 고르기).

    목록의 항목은 `소유자/이름` 꼴이라 뒷부분만 견준다 — 사용자가 담아 둔 것은
    저장소 이름뿐이고, 소유자는 로그인한 본인이거나 그 설치의 주인이다.
    """
    if not hint:
        return None
    matches = [full for full in repos if full.rsplit("/", 1)[-1] == hint]
    # 이름이 같은 저장소가 둘 이상이면(개인 계정과 조직에 같은 이름) 어느 쪽인지
    # 알 수 없다 — 지어내지 않고 평소대로 사용자에게 고르게 한다.
    return matches[0] if len(matches) == 1 else None


async def repo_step(request: Request) -> Response:
    """2단계 — 기억 저장소 마련하기."""
    user_key = _session_user_key(request)
    if not user_key:
        return HTMLResponse(_html_me_login_required(), status_code=401)

    # 이미 연결을 끝낸 사람이 이 주소를 다시 열면 막다른 길이 된다(할 일이 없는
    # 화면이라 다음 버튼이 전부 제자리걸음이다) — 내 페이지로 보낸다.
    with closing(identity.connect()) as conn:
        row = identity.get_by_user_key(conn, user_key)
    if row and row.get("installation_id") and row.get("repo_full_name"):
        return RedirectResponse(url="/auth/me", status_code=302)

    return HTMLResponse(_html_repo_step())


async def repo_done(request: Request) -> Response:
    """"만들었어요, 다음" — 저장소 이름을 담아 두고 3단계(권한 주기)로 넘긴다."""
    if not _session_user_key(request):
        return HTMLResponse(_html_me_login_required(), status_code=401)

    resp = RedirectResponse(url=_INSTALL_PATH, status_code=302)
    resp.set_cookie(
        _REPO_HINT_COOKIE_NAME,
        _sign_with_expiry(_DEFAULT_NEW_REPO_NAME, _REPO_HINT_TTL_SEC),
        max_age=_REPO_HINT_TTL_SEC,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/auth",
    )
    return resp


async def select_repo(request: Request) -> Response:
    user_key = _session_user_key(request)
    if not user_key:
        return PlainTextResponse(
            "로그인이 필요합니다 — 세션이 없거나 만료됐습니다. "
            "Login required: session missing or expired.",
            status_code=401,
        )

    installation_id_raw = request.query_params.get("installation_id") or ""
    repo = request.query_params.get("repo") or ""
    sig = request.query_params.get("sig") or ""
    try:
        installation_id = int(installation_id_raw)
    except ValueError:
        return PlainTextResponse(
            "installation_id가 올바르지 않습니다. Invalid installation_id.", status_code=400
        )

    # repo를 그대로 믿지 않는다 — 콜백 단계에서 조회해 이 링크에 실어 보낸
    # 서명(user_key+installation_id+repo 묶음)을 재계산해 대조한다. 사용자가
    # URL을 손으로 고쳐 남의 저장소 이름을 밀어 넣어도 서명이 어긋나 통과하지
    # 못한다.
    expected_sig = _repo_link_sig(user_key, installation_id, repo)
    if not sig or not hmac.compare_digest(sig, expected_sig):
        logger.warning("select-repo 서명 검증 실패 (user_key=%s)", user_key)
        return PlainTextResponse(
            "요청이 유효하지 않습니다(서명 불일치) — 콜백 화면에서 받은 링크를 그대로 "
            "다시 이용하세요. Invalid request: signature mismatch.",
            status_code=403,
        )

    try:
        with closing(identity.connect()) as conn:
            identity.set_installation(conn, user_key, installation_id, repo)
            mcp_url = _mcp_url_for(request, conn, user_key)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)

    return HTMLResponse(_html_connected(user_key, repo, mcp_url))


async def me(request: Request) -> Response:
    """내 페이지(namu-60) — 로그인한 본인의 접속 주소를 로그인 왕복 직후가
    아니어도 다시 볼 수 있는 유일한 영구 경로.

    세션이 없거나(로그인 안 함) 서명이 위조/만료됐으면, 또는 서명은 유효한데
    장부에 그 사용자가 없으면(장부 재구축 등) 전부 같은 취급 — 본인 정보를
    한 글자도 내보내지 않고 401로 로그인 안내만 준다. "왜 막혔는지"를 구분해
    알려주면 공격자에게 열거 단서를 준다(select_repo의 서명 실패 처리와 같은
    원칙).
    """
    user_key = _session_user_key(request)
    if not user_key:
        return HTMLResponse(_html_me_login_required(), status_code=401)
    with closing(identity.connect()) as conn:
        return _me_page_response(request, conn, user_key)


def _me_page_response(
    request: Request, conn, user_key: str, notice_html: str = ""
) -> Response:
    """내 페이지 본문을 만들어 응답으로 돌려준다.

    GET `/auth/me`와 주소 관리 POST 3종이 **같은 렌더링 경로**를 쓴다 — 동작을
    끝낸 뒤 사용자가 보게 되는 화면이 평소 내 페이지와 다르면(예: 재발급 후
    별도 화면) 새 주소를 그 자리에서 확인할 수 없거나 두 화면이 따로 놀게 된다.
    바뀌는 것은 맨 위에 붙는 알림 한 줄(notice_html)뿐이다.
    """
    row = identity.get_by_user_key(conn, user_key)
    if row is None:
        # 서명은 유효했지만(즉 우리가 발급한 세션) 장부에 없다 — 500으로
        # 터뜨리지 않고 로그인 안내와 동일하게 처리한다.
        return HTMLResponse(_html_me_login_required(), status_code=401)

    if not row.get("installation_id") or not row.get("repo_full_name"):
        return HTMLResponse(_html_me_not_connected(user_key, notice_html))

    revoked = bool(row.get("mcp_revoked_at"))
    mcp_url = _mcp_url_for(request, conn, user_key)
    if not mcp_url and not revoked:
        # 저장소는 연결됐는데 mcp_secret이 없는 옛 계정 — 화면을 여는
        # 시점에 발급해 저장한다. identity.backfill_mcp_secrets는 이미
        # 값이 있는 사용자는 절대 건드리지 않고(WHERE mcp_secret IS NULL
        # OR ''), UNIQUE 색인이 있는 컬럼에 새로 굴린 무작위값만 채우므로
        # 동시 요청이 겹쳐도(SQLite가 쓰기를 직렬화) 예외로 깨지지 않는다
        # — 새 발급/백필 함수를 새로 만들지 않고 기존 것을 그대로 재사용.
        # 스스로 폐기한 사용자(revoked)는 여기 들어오면 안 된다 — 폐기가 곧바로
        # 취소되기 때문이다(backfill 쪽에도 같은 조건이 걸려 있다, 이중 방어).
        identity.backfill_mcp_secrets(conn)
        mcp_url = _mcp_url_for(request, conn, user_key)

    return HTMLResponse(
        _html_me_connected(
            user_key,
            row["repo_full_name"],
            mcp_url,
            notice_html=notice_html,
            revoked=revoked,
        )
    )


# ---------------------------------------------------------------------------
# 기억 열람·검색 + 메모 떼기 + 열린 작업 보드 (namu-60 완료조건 4·5·7)
#
# 그릇은 네 개다 — 교훈(learnings)·개인 사실(profile)·쪽지(memo)·작업일지(tasks).
#
# 읽는 데이터의 출처: 회원 저장소에는 `tasks/<프로젝트>/<작업>/{task.md,log.md}`가
# 통째로 올라오고, user_repo가 그것을 `STORE_ROOT/users/<키>/`로 복제한다. 이 화면은
# 그 복제본을 읽을 뿐이고 컨테이너 홈은 건드리지 않는다. (2026-08-01 이전 주석은
# "서버에 데이터가 없어 완료조건 7은 못 채운다"고 단정했다 — 틀린 판단이었다.)
#
# 작업일지 **쓰기**는 2026-08-02부터 도구 쪽(routing_server)에서 가능해졌다. 이
# 화면이 여전히 열람만인 것은 막혀서가 아니라 화면의 역할이 열람이기 때문이다.
#
# 그릇마다 화면에서 되는 일이 다르다는 것이 이 화면의 핵심 요구다:
#   learnings — 열람만(고치거나 지울 수 없다. 쌓인 배움을 사후에 손대면 기록의
#               값어치가 사라진다)
#   profile   — 열람만(정정은 supersedes로 새 항목을 쌓는 방식이라 대화에서 한다)
#   memo      — 유일하게 **실제로 지워지는** 그릇이라 체크박스로 뗄 수 있다
#   tasks     — 이 화면에서는 열람만(기록은 대화 중에 도구로 남긴다)
# 화면은 이 차이를 문구로 설명하는 데서 그치지 않고, 뗄 수 있는 그릇에만 폼을
# 그린다(설명만 다르고 버튼은 다 있으면 결국 눌러 보고 알게 된다).
# ---------------------------------------------------------------------------
#   attachments — 열람만(올린 파일의 이력. 파일 몸통은 이 화면에 없다 —
#               저장소의 attach_file/에 있고 그 폴더는 각 PC로 안 내려온다)
_MEMORY_BOWLS = ("learnings", "profile", "memo", "tasks", "attachments")

_BOWL_LABEL = {
    "learnings": "교훈",
    "profile": "개인 사실",
    "memo": "쪽지",
    "tasks": "작업일지",
    "attachments": "첨부 기록",
}

# 한 화면에 올리는 최대 건수. 페이지 넘기기는 이번 범위가 아니라, 넘치면 "더
# 있습니다"라고 알리고 검색으로 좁히게 안내한다(조용히 자르지 않는다).
_MEMORY_PAGE_LIMIT = 30

_core_modules = None


def _core():
    """vendor 코어(config/db/memo/profile)를 지연 로드한다.

    모듈 로드 시점에 얹지 않는 이유: `sys.path` 맨 앞에 vendor를 끼우는 부작용을
    web_auth를 import하는 것만으로 일으키면, 이름이 겹치는 모듈(access_log 등)이
    어느 쪽으로 풀릴지가 import 순서에 좌우된다. routing_server가 이미 같은
    경로를 얹으므로 실서버에서는 여기서 하는 일이 없고, 테스트가 web_auth만
    단독으로 import할 때만 실제로 얹힌다.
    """
    global _core_modules
    if _core_modules is None:
        import sys
        from pathlib import Path

        vendor_plugin = (
            Path(__file__).resolve().parent.parent
            / "vendor"
            / "namu-agent"
            / "namu-plugin"
        )
        if str(vendor_plugin) not in sys.path:
            sys.path.append(str(vendor_plugin))
        import config as cfg
        import db
        import memo
        import profile

        _core_modules = (cfg, db, memo, profile)
    return _core_modules


def _core_tasks():
    """작업일지 읽기용 코어 모듈(`task_resolve`).

    `_core()`가 돌려주는 네 모듈 묶음에 끼워 넣지 않는다 — 그 튜플은 이미 여러
    곳에서 `cfg, db, memo, profile = _core()`로 펼쳐 받고 있어, 한 칸 늘리는 것이
    작업일지와 상관없는 모든 호출부를 고치는 일이 된다. sys.path를 얹는 일은
    `_core()`가 이미 하므로 여기서는 먼저 부르기만 한다.
    """
    _core()
    import task_resolve

    return task_resolve


def _core_attachments():
    """첨부 기록 읽기용 코어 모듈(`attachments`).

    `_core()`의 네 모듈 묶음에 끼우지 않는 이유는 `_core_tasks`와 같다 — 그 튜플은
    여러 곳에서 `cfg, db, memo, profile = _core()`로 펼쳐 받고 있어, 한 칸 늘리면
    첨부와 상관없는 호출부를 전부 고쳐야 한다.
    """
    _core()
    import attachments

    return attachments


def _memory_paths(user_key: str):
    """그 사용자의 기억 파일 묶음(DataPaths).

    폴더는 `user_repo.user_dir`로 구한다 — routing_server가 읽고 쓰는 바로 그
    폴더이며, 경로 탈출 이중 차단(슬러그 검증 + resolve 후 재확인)이 그 함수
    안에 이미 들어 있다. 여기서 경로를 새로 조립하면 그 방어선이 갈라진다.
    """
    cfg, _db, _memo, _profile = _core()
    return cfg.data_paths_for(user_repo.user_dir(user_key))


def _ensure_learnings_cache(paths) -> None:
    """교훈 조회용 캐시(sqlite)가 없거나 낡았으면 yaml에서 다시 만든다.

    routing_server._ensure_fresh와 같은 처리다 — 웹에서 먼저 열어 본 사용자의
    화면이 비어 보이지 않게 하려면 이 화면도 같은 보정을 해야 한다.
    """
    _cfg, db, _memo, _profile = _core()
    if not paths.db_path.exists() or db.cache_is_stale(
        paths.learnings_yaml, paths.db_path
    ):
        db.rebuild_from_yaml(paths=paths)


def _matches(query: str, *texts: str) -> bool:
    """쪽지·개인 사실용 단순 부분일치. 교훈과 달리 이 두 그릇은 색인이 없다."""
    q = query.strip().lower()
    if not q:
        return True
    return any(q in (t or "").lower() for t in texts)


def _html_layers(summary: str, reason: str, body: str, meta_html: str = "") -> str:
    """3층(요약·왜·상세)을 한 덩어리로 그린다.

    요약만 굵게 세우고 왜는 그 아래 한 문단, 상세는 접어 둔다 — 목록에서 상세를
    펼쳐 두면 항목 하나가 화면을 다 먹어 "훑어보기"가 불가능해진다.
    """
    parts = [f"<p class=\"m-sum\"><b>{html.escape(summary or '(요약 없음)')}</b></p>"]
    if reason:
        parts.append(f"<p class=\"m-why\">{html.escape(reason)}</p>")
    if body:
        parts.append(
            "<details><summary>상세 보기</summary>"
            f"<pre class=\"m-body\">{html.escape(body)}</pre></details>"
        )
    if meta_html:
        parts.append(f"<p class=\"m-meta\"><small>{meta_html}</small></p>")
    return "".join(parts)


def _html_learnings(rows: list) -> str:
    if not rows:
        return ""
    items = []
    for row in rows:
        summary, reason, body = (
            str(row.get("summary") or row.get("statement") or ""),
            str(row.get("reason") or row.get("source") or ""),
            str(row.get("body") or ""),
        )
        meta_bits = []
        for key in ("created_at", "ts", "date"):
            if row.get(key):
                meta_bits.append(html.escape(str(row[key])))
                break
        for key in ("topic", "task"):
            if row.get(key):
                meta_bits.append(html.escape(str(row[key])))
                break
        if row.get("outcome"):
            meta_bits.append(html.escape(str(row["outcome"])))
        items.append(
            '<li class="m-item">'
            + _html_layers(summary, reason, body, " · ".join(meta_bits))
            + "</li>"
        )
    return f'<ul class="m-list">{"".join(items)}</ul>'


def _html_profile(docs: list) -> str:
    if not docs:
        return ""
    _cfg, _db, _memo, profile = _core()
    items = []
    for doc in docs:
        summary, reason, body = profile.layers(doc)
        meta_bits = []
        if doc.get("subject") or doc.get("topic"):
            meta_bits.append(html.escape(str(doc.get("subject") or doc.get("topic"))))
        for tag in doc.get("tags") or []:
            meta_bits.append(html.escape(str(tag)))
        items.append(
            '<li class="m-item">'
            + _html_layers(summary, reason, body, " · ".join(meta_bits))
            + "</li>"
        )
    return f'<ul class="m-list">{"".join(items)}</ul>'


def _html_memo(entries: list, short_by_id: dict) -> str:
    """쪽지 목록 — 이 그릇만 체크박스가 붙는다(유일하게 실제로 지워지는 그릇).

    표시 id는 목록 안에서 서로 구별되는 최단 접두(`memo.short_ids`)를 쓴다.
    폼이 실제로 보내는 값은 전체 id다 — 화면에 짧게 보이는 것과 서버가 받는
    것을 같게 만들면 목록이 바뀔 때마다 지우는 대상이 달라진다.
    """
    if not entries:
        return ""
    _cfg, _db, memo, _profile = _core()
    items = []
    for entry in entries:
        full_id = str(entry.get("id") or "")
        short = short_by_id.get(full_id, full_id)
        summary, reason, body = memo.layers(entry)
        meta_bits = [f"id <code>{html.escape(short)}</code>"]
        if entry.get("created_at"):
            meta_bits.append(html.escape(str(entry["created_at"])))
        if entry.get("via"):
            meta_bits.append(html.escape(str(entry["via"])))
        items.append(
            '<li class="m-item">'
            f'<label class="m-pick"><input type="checkbox" name="memo_id" '
            f'value="{html.escape(full_id)}"> 떼기</label>'
            + _html_layers(summary, reason, body, " · ".join(meta_bits))
            + "</li>"
        )
    return (
        '<form method="post" action="/auth/memo/remove">'
        f'<ul class="m-list">{"".join(items)}</ul>'
        '<p><button type="submit" class="btn">고른 쪽지 떼기</button> '
        "<small>뗀 쪽지는 저장소에서 실제로 사라집니다(되돌릴 수 없습니다).</small>"
        "</p></form>"
    )


def _fmt_bytes(size) -> str:
    """사람이 읽는 크기. 화면에 284915라고 적으면 큰지 작은지 알 수 없다."""
    try:
        n = int(size)
    except (TypeError, ValueError):
        return "크기 모름"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _html_attachments(entries: list) -> str:
    """첨부 기록 목록 — 어떤 파일이 어떤 경로로 올라가고 지워졌는지.

    다른 그릇과 달리 **경로가 요약보다 먼저 눈에 와야 한다**. 이 화면을 보는 이유가
    "무슨 파일이 있더라"이고, 그 답은 요약문이 아니라 파일 이름이기 때문이다.

    크기는 기록의 `bytes` 칸에서만 읽는다. 저장소에 물으면 git이 크기를 알아내려고
    빠진 파일 몸통을 전부 내려받아 첨부 격리가 뚫린다(2026-08-07 실측).
    """
    if not entries:
        return ""
    items = []
    for entry in entries:
        path = str(entry.get("path") or "(경로 없음)")
        status = str(entry.get("status") or "")
        parts = [
            f'<p class="m-sum"><code>{html.escape(path)}</code> '
            f"<b>{html.escape(status)}</b></p>"
        ]
        if entry.get("summary"):
            parts.append(f'<p>{html.escape(str(entry["summary"]))}</p>')
        if entry.get("reason"):
            parts.append(f'<p class="m-why">{html.escape(str(entry["reason"]))}</p>')
        if entry.get("body"):
            parts.append(
                "<details><summary>상세 보기</summary>"
                f'<pre class="m-body">{html.escape(str(entry["body"]))}</pre></details>'
            )
        meta_bits = [_fmt_bytes(entry.get("bytes"))]
        if entry.get("timestamp"):
            meta_bits.append(html.escape(str(entry["timestamp"])[:19].replace("T", " ")))
        for key in ("task", "project"):
            if entry.get(key):
                meta_bits.append(html.escape(str(entry[key])))
        for tag in entry.get("tags") or []:
            meta_bits.append(html.escape(str(tag)))
        parts.append(f'<p class="m-meta"><small>{" · ".join(meta_bits)}</small></p>')
        items.append('<li class="m-item">' + "".join(parts) + "</li>")
    return f'<ul class="m-list">{"".join(items)}</ul>'


# ---------------------------------------------------------------------------
# 열린 작업 보드 (namu-60 완료조건 7)
#
# "CLI 브리핑과 같은 내용"이 완료조건 문구다. 그래서 무엇이 열린 작업인지·어떤
# 순서로 세우는지를 여기서 새로 정하지 않고, 코어(task_resolve)의 판정 함수를
# 그대로 부른다 — 닫힘 판정(context의 `(완료)` 우선, 없으면 log 폴백)이나 책갈피
# 우선 순서를 이 파일에 다시 구현하면 규칙이 두 벌로 갈려 웹과 CLI가 서로 다른
# 목록을 보여주게 된다(코어가 `find_open_tasks` 주석에서 못박은 실패 양식이다).
#
# 코어의 `open_tasks_briefing()`을 통째로 못 쓰는 이유: 그 함수는 프로젝트 목록을
# `~/.namu`(컨테이너 홈) 기준으로 스스로 훑는다. 클라우드는 사용자마다 뿌리가
# 달라야 하므로, 뿌리를 인자로 받는 하위 함수들만 골라 쓰고 프로젝트를 훑는 한
# 겹만 여기서 만든다.
# ---------------------------------------------------------------------------
def _task_project_dirs(user_key: str) -> list:
    """그 사용자 저장소 사본의 `tasks/<프로젝트>` 폴더 목록.

    회원 저장소에 작업 기록이 한 번도 올라온 적 없으면 폴더 자체가 없다 — 이것은
    오류가 아니라 "아직 없음"이므로 빈 목록으로 돌려준다.
    """
    tasks_root = user_repo.user_dir(user_key) / "tasks"
    try:
        return sorted(d for d in tasks_root.iterdir() if d.is_dir())
    except OSError:
        return []


def _task_last_ts(task_dir) -> "str | None":
    """그 작업 log.md의 가장 늦은 기록 시각(정렬용). 없으면 None.

    코어의 비공개 함수를 부른다 — 시각을 뽑는 규칙(줄 순서가 아니라 최댓값을
    고른다)을 여기에 베껴 오면 시간대가 다른 호스트가 남긴 줄에서 웹과 CLI의
    정렬이 갈린다. 이 이름이 사라지면 `test_web_auth.py`의 전용 시험이 먼저
    실패해, 코어를 올릴 때 조용히 어긋나지 않는다.
    """
    ts = _core_tasks()._latest_log_ts(task_dir / "log.md")
    return f"{ts[0]} {ts[1]}" if ts else None


def _open_task_rows(user_key: str, query: str = "") -> list:
    """열린 작업 전부를 브리핑과 같은 순서(책갈피 먼저, 그다음 최근 활동순)로."""
    tr = _core_tasks()
    rows = []
    for project_dir in _task_project_dirs(user_key):
        pins = tr.pins_by_slug(project_dir)
        for task_dir in tr.find_open_tasks(project_dir):
            pin = pins.get(task_dir.name)
            rows.append(
                {
                    "project": project_dir.name,
                    "slug": task_dir.name,
                    "title": tr.task_title(task_dir),
                    "next": tr.next_note(task_dir),
                    "why": tr.next_why(task_dir),
                    "last_ts": _task_last_ts(task_dir),
                    "pin_machine": pin["machine"] if pin else None,
                    "pin_ts": pin["ts"] if pin else None,
                }
            )

    rows.sort(
        key=lambda r: (r["pin_ts"] is not None, r["pin_ts"] or "", r["last_ts"] or ""),
        reverse=True,
    )
    if query:
        rows = [
            r
            for r in rows
            if _matches(
                query, r["project"], r["slug"], r["title"], r["next"] or "", r["why"] or ""
            )
        ]
    return rows


def _html_task_board(rows: list) -> str:
    """열린 작업 목록 — 각 항목의 주인공은 제목이 아니라 **다음에 할 일**이다.

    브리핑에서 이 화면을 보는 목적이 "어디서부터 이어서 하지?"이기 때문에, 다음
    줄은 접지 않고 자르지도 않는다(잘리면 재진입 지점의 의미가 없어진다). 대신
    분량이 들쭉날쭉한 '왜' 한 줄만 접어 둔다.
    """
    if not rows:
        return ""
    items = []
    for row in rows:
        head = html.escape(row["title"] or row["slug"])
        if row["pin_machine"]:
            head = "📌 " + head
        next_text = (row["next"] or "").strip()
        if next_text:
            next_html = (
                '<p class="m-why"><b>다음:</b> ' + html.escape(next_text) + "</p>"
            )
        else:
            next_html = (
                '<p class="m-why">다음에 할 일이 아직 적혀 있지 않습니다 — '
                "AI와 작업을 이어가면 여기에 채워집니다.</p>"
            )

        why_html = ""
        if (row["why"] or "").strip():
            why_html = (
                "<details><summary>왜 거기서부터인지</summary>"
                f'<pre class="m-body">{html.escape(row["why"].strip())}</pre></details>'
            )

        meta_bits = [html.escape(row["project"]), f"<code>{html.escape(row['slug'])}</code>"]
        if row["last_ts"]:
            meta_bits.append("마지막 기록 " + html.escape(row["last_ts"]))
        if row["pin_machine"]:
            meta_bits.append("책갈피 " + html.escape(row["pin_machine"]))

        items.append(
            '<li class="m-item">'
            f'<p class="m-sum"><b>{head}</b></p>'
            + next_html
            + why_html
            + f'<p class="m-meta"><small>{" · ".join(meta_bits)}</small></p>'
            + "</li>"
        )
    return f'<ul class="m-list">{"".join(items)}</ul>'


# 내 기억 화면에만 필요한 조각들. 공통 부품(ui.py)에 올리지 않는 이유는 이 화면
# 밖에서 쓸 일이 없기 때문이다 — 색과 모서리 값은 ui.py가 정한 것을 그대로 쓴다.
_MEMORY_CSS = (
    "<style>"
    ".m-tabs{margin:0 0 16px;padding:0;list-style:none;display:flex;flex-wrap:wrap;gap:8px}"
    ".m-tabs a{display:inline-block;padding:6px 12px;border:1px solid var(--border);"
    "border-radius:999px;text-decoration:none;background:var(--bg-card);"
    "font-size:.9rem;font-weight:600}"
    ".m-tabs a.on{background:var(--accent);border-color:var(--accent);"
    "color:var(--on-accent)}"
    ".m-list{list-style:none;padding:0;margin:0}"
    ".m-item{border:1px solid var(--border);border-radius:10px;padding:12px 14px;"
    "background:var(--bg-card);margin:0 0 12px}"
    ".m-sum{margin:0 0 6px}"
    ".m-why{margin:0 0 6px;opacity:.85}"
    ".m-meta{margin:6px 0 0;opacity:.7}"
    ".m-body{white-space:pre-wrap;word-break:break-word;overflow-x:auto;margin:8px 0 0}"
    ".m-pick{float:right;margin-left:12px}"
    "</style>"
)


def _html_memory_page(
    bowl: str,
    query: str,
    listing_html: str,
    *,
    count: int,
    notice_html: str = "",
) -> str:
    tabs = []
    for name in _MEMORY_BOWLS:
        cls = ' class="on"' if name == bowl else ""
        q = f"&q={_urlquote(query)}" if query else ""
        tabs.append(
            f'<li><a href="/auth/memory?bowl={name}{q}"{cls}>'
            f"{html.escape(_BOWL_LABEL[name])}</a></li>"
        )

    if bowl == "tasks":
        rule = (
            "작업일지는 <b>이 화면에서는 보기만 합니다</b> — 기록을 남기는 것은 "
            "회원님 PC의 나무와, 이 주소를 붙인 AI가 합니다(남길 때는 어느 "
            "프로젝트인지 함께 적습니다). 이 목록은 그 기록을 그대로 옮겨 온 "
            "것이라, PC에서 보시는 브리핑과 같은 내용입니다."
        )
    elif bowl == "memo":
        rule = (
            "쪽지는 <b>쓰고 버리는 그릇</b>이라 다섯 그릇 중 유일하게 화면에서 실제로 "
            "지울 수 있습니다."
        )
    elif bowl == "profile":
        rule = (
            "개인 사실은 <b>화면에서 고치거나 지울 수 없습니다.</b> 틀린 내용은 "
            "AI에게 말해 새 사실로 정정하시면, 옛 항목이 자동으로 물러납니다."
        )
    elif bowl == "attachments":
        rule = (
            "올리신 파일의 <b>이력</b>입니다 — 파일 자체는 여기 없고 회원님 저장소에 "
            "있습니다. 올림·새 판·지움이 모두 남으므로 <b>지운 파일도 목록에 "
            "보입니다</b>(무엇이 있었고 왜 뺐는지가 남습니다). 파일을 올리고 "
            "지우는 일은 AI와 대화하며 하시면 됩니다."
        )
    else:
        rule = (
            "교훈은 <b>화면에서 고치거나 지울 수 없습니다.</b> 쌓인 배움을 나중에 "
            "손대면 그때 무엇을 알았는지가 남지 않기 때문입니다."
        )

    if listing_html:
        body_html = listing_html
        if count >= _MEMORY_PAGE_LIMIT:
            body_html += (
                f"<p><small>가장 최근 {_MEMORY_PAGE_LIMIT}건만 보여드렸습니다 — "
                "더 있는 경우 위 검색창으로 좁혀 찾으세요.</small></p>"
            )
    elif query:
        body_html = (
            f"<p><b>'{html.escape(query)}'</b>에 해당하는 "
            f"{html.escape(_BOWL_LABEL[bowl])}이(가) 없습니다.</p>"
        )
    elif bowl == "tasks":
        # 작업일지는 웹에서 만들 수 없는 그릇이라, 다른 그릇의 "AI와 대화하며
        # 남기시면 쌓입니다"를 그대로 쓰면 여기서 할 수 없는 일을 안내하게 된다.
        body_html = (
            "<p>지금 열려 있는 작업이 없습니다 — 회원님 PC의 나무에서 작업을 "
            "시작하시면 여기에 나타납니다.</p>"
        )
    elif bowl == "attachments":
        body_html = (
            "<p>아직 올리신 파일이 없습니다 — AI에게 “이 파일도 나무에 올려줘”라고 "
            "하시면 여기에 쌓입니다.</p>"
        )
    else:
        body_html = (
            f"<p>아직 {html.escape(_BOWL_LABEL[bowl])}이(가) 없습니다 — AI와 "
            "대화하며 기억을 남기시면 여기에 쌓입니다.</p>"
        )

    body = (
        _MEMORY_CSS
        + "<h1>내 기억 (My memory)</h1>"
        + notice_html
        + f'<ul class="m-tabs">{"".join(tabs)}</ul>'
        + '<form method="get" action="/auth/memory">'
        + f'<input type="hidden" name="bowl" value="{html.escape(bowl)}">'
        + f'<input type="text" name="q" value="{html.escape(query)}" '
        'placeholder="찾을 낱말"> '
        '<button type="submit" class="btn">찾기</button>'
        + (' <a href="/auth/memory?bowl=' + bowl + '">전체 보기</a>' if query else "")
        + "</form>"
        + f"<p><small>{rule}</small></p>"
        + body_html
        + '<p><a href="/auth/me">← 내 페이지로</a></p>'
    )
    return _html_page("NAMU 내 기억", body)


def _urlquote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def _load_bowl(user_key: str, bowl: str, query: str) -> "tuple[str, int]":
    """그릇 하나를 읽어 (목록 HTML, 건수)를 돌려준다."""
    _cfg, db_mod, memo, profile = _core()
    paths = _memory_paths(user_key)

    if bowl == "tasks":
        # 다른 그릇과 달리 뒤에서 자르지 않는다 — 이미 "책갈피 먼저, 그다음 최근
        # 활동순"으로 세워져 있어 앞쪽이 가장 급한 작업이다.
        rows = _open_task_rows(user_key, query)[:_MEMORY_PAGE_LIMIT]
        return _html_task_board(rows), len(rows)

    if bowl == "memo":
        entries = [e for e in memo.load_all(paths=paths)]
        if query:
            entries = [
                e for e in entries if _matches(query, *memo.layers(e))
            ]
        entries = entries[-_MEMORY_PAGE_LIMIT:]
        short_by_id = memo.short_ids(entries)
        return _html_memo(entries, short_by_id), len(entries)

    if bowl == "profile":
        docs = profile.active(paths=paths)
        if query:
            docs = [d for d in docs if _matches(query, *profile.layers(d))]
        docs = docs[-_MEMORY_PAGE_LIMIT:]
        return _html_profile(docs), len(docs)

    if bowl == "attachments":
        # 지운 것까지 전부 보여준다 — 이 화면을 보는 이유의 절반이 "그 파일 어디
        # 갔지"이고, 살아 있는 것만 보이면 그 질문에 답할 수 없다.
        attachments = _core_attachments()
        entries = attachments.load_all(paths=paths)
        if query:
            # 파일 이름(path)도 훑는다 — 다시 찾을 때 기억나는 것은 대개 이름이다.
            entries = [
                e for e in entries
                if _matches(
                    query,
                    str(e.get("path") or ""), str(e.get("summary") or ""),
                    str(e.get("reason") or ""), str(e.get("body") or ""),
                    str(e.get("task") or ""), str(e.get("project") or ""),
                )
            ]
        entries = list(reversed(entries))[:_MEMORY_PAGE_LIMIT]
        return _html_attachments(entries), len(entries)

    _ensure_learnings_cache(paths)
    with closing(sqlite3.connect(paths.db_path)) as conn:
        if query:
            # 검색은 db.search를 쓴다 — db.recall은 결과가 없으면 최신 목록으로
            # 물러나므로, 찾는 말이 없는데도 뭔가 나온 것처럼 보인다.
            rows = db_mod.search(conn, query, None, _MEMORY_PAGE_LIMIT).get(
                "results", []
            )
        else:
            rows = db_mod.recall(conn, None, None, _MEMORY_PAGE_LIMIT)
    return _html_learnings(rows), len(rows)


def _memory_response(
    request: Request,
    user_key: str,
    notice_html: str = "",
    bowl_override: "str | None" = None,
) -> Response:
    # POST(쪽지 떼기)에는 쿼리 문자열이 없다 — 그대로 두면 뗀 직후에 교훈 그릇이
    # 열려 "방금 뭘 지웠는지" 확인할 자리가 사라진다. 동작을 한 그릇에서 시작했으면
    # 결과도 그 그릇에서 보여준다.
    bowl = (bowl_override or request.query_params.get("bowl") or "learnings").strip()
    if bowl not in _MEMORY_BOWLS:
        bowl = "learnings"
    query = (request.query_params.get("q") or "").strip()

    with closing(identity.connect()) as conn:
        row = identity.get_by_user_key(conn, user_key)
        if row is None:
            return HTMLResponse(_html_me_login_required(), status_code=401)
        if not row.get("installation_id") or not row.get("repo_full_name"):
            return HTMLResponse(_html_me_not_connected(user_key))
        try:
            # 읽기 전에 회원 저장소 복제를 최신으로 맞춘다 — 다른 기기·웹 AI가
            # 남긴 기억이 화면에만 안 보이는 상태를 만들지 않기 위해서다.
            user_repo.ensure_ready(conn, user_key)
        except user_repo.UserRepoError as exc:
            logger.warning("기억 화면: 저장소 준비 실패 (user_key=%s): %s", user_key, exc)
            notice_html += _html_notice(
                "저장소를 최신으로 맞추지 못해 <b>마지막으로 받아 둔 내용</b>을 "
                "보여드립니다.",
                tone="warn",
            )

    try:
        listing_html, count = _load_bowl(user_key, bowl, query)
    except (OSError, ValueError, sqlite3.Error) as exc:
        logger.warning("기억 화면: %s 그릇 읽기 실패 (user_key=%s): %s", bowl, user_key, exc)
        listing_html, count = "", 0
        notice_html += _html_notice(
            "기억을 읽지 못했습니다 — 잠시 후 새로고침해 보세요.", tone="warn"
        )

    return HTMLResponse(
        _html_memory_page(
            bowl, query, listing_html, count=count, notice_html=notice_html
        )
    )


async def memory(request: Request) -> Response:
    """기억 열람·검색 화면(namu-60 완료조건 4)."""
    user_key = _session_user_key(request)
    if not user_key:
        return HTMLResponse(_html_me_login_required(), status_code=401)
    return await run_in_threadpool(_memory_response, request, user_key)


def _memo_remove_sync(request: Request, user_key: str, memo_ids: list) -> Response:
    _cfg, _db, memo, _profile = _core()
    paths = _memory_paths(user_key)

    removed = 0
    missing = 0
    for memo_id in memo_ids:
        try:
            memo.remove(memo_id, paths=paths)
            removed += 1
        except (KeyError, ValueError, LookupError):
            # 이미 없는 id(다른 기기에서 먼저 뗐거나 새로고침 후 재제출) — 실패로
            # 취급하지 않는다. 결과가 같으므로 사용자에게 오류를 보일 이유가 없다.
            missing += 1
        except OSError as exc:
            logger.warning("쪽지 떼기 실패 (user_key=%s, id=%s): %s", user_key, memo_id, exc)

    notice = ""
    if removed:
        # 뗀 결과를 회원 저장소에 밀어 넣지 않으면, 다음 최신화 때 원격 내용이
        # 다시 내려와 **뗀 쪽지가 부활한다**(완료조건 5가 못박은 실패 양식).
        try:
            with closing(identity.connect()) as conn:
                user_repo.push(conn, user_key, "쪽지 떼기(웹)")
            notice = _html_notice(f"쪽지 {removed}장을 뗐습니다.", tone="good")
        except user_repo.UserRepoError as exc:
            logger.warning("쪽지 떼기 후 push 실패 (user_key=%s): %s", user_key, exc)
            notice = _html_notice(
                f"쪽지 {removed}장을 이 서버에서는 뗐지만 <b>회원님 저장소에 "
                "반영하지 못했습니다.</b> 잠시 후 다시 시도해 주세요 — 그때까지는 "
                "다른 기기에서 다시 나타날 수 있습니다.",
                tone="warn",
            )
    elif missing:
        notice = _html_notice("고르신 쪽지는 이미 떼어져 있습니다.", tone="info")
    else:
        notice = _html_notice("뗄 쪽지를 고르지 않으셨습니다.", tone="info")

    return _memory_response(request, user_key, notice_html=notice, bowl_override="memo")


async def memo_remove(request: Request) -> Response:
    """쪽지 떼기(namu-60 완료조건 5) — POST 전용.

    파괴적 동작이라 GET으로 두지 않는다(주소 재발급·폐기와 같은 원칙). 세션
    검증도 새 경로를 만들지 않고 `_session_user_key`를 그대로 쓴다.
    """
    user_key = _session_user_key(request)
    if not user_key:
        return PlainTextResponse(
            "로그인이 필요합니다 — 세션이 없거나 만료됐습니다. "
            "Login required: session missing or expired.",
            status_code=401,
        )
    form = await request.form()
    memo_ids = [str(v).strip() for v in form.getlist("memo_id") if str(v).strip()]
    return await run_in_threadpool(_memo_remove_sync, request, user_key, memo_ids)


# ---------------------------------------------------------------------------
# 주소 관리(namu-60) — 연결 시험 / 재발급 / 폐기. 셋 다 **POST 전용**이다.
#
# GET으로 두면 링크 프리페치나 채팅 미리보기 크롤러가 눌러 버릴 수 있고,
# 재발급·폐기는 그 순간 사용자의 커넥터가 끊기는 파괴적 동작이다.
# build_auth_app의 methods=["POST"]가 이 규약을 강제한다(GET이면 405).
#
# 세션 검증은 새 경로를 만들지 않고 `_session_user_key`(내 페이지와 동일)를
# 그대로 쓴다. 다른 사이트에서 보낸 POST(CSRF)는 세션 쿠키가 SameSite=Lax라
# 애초에 쿠키가 실리지 않아 여기서 401로 끊긴다.
# ---------------------------------------------------------------------------
def _wants_json(request: Request) -> bool:
    """화면 안에서 그 자리에 결과만 받아 가려는 요청인가(namu-69).

    별도 경로(`/auth/mcp/test.json` 등)를 새로 만들지 않는 이유: 경로가 늘면
    세션 검증·CSRF 성질(POST 전용, SameSite=Lax)을 두 곳에서 지켜야 하고, 한쪽만
    고치는 사고가 이 프로젝트에서 반복됐다. 같은 경로가 **같은 판정**을 하고
    포장지만 바꾼다.
    """
    return "application/json" in (request.headers.get("accept") or "").lower()


def _test_result_response(request: Request, notice_html: str, status_code: int = 200):
    """연결 시험의 결과 포장지 — 화면 안 요청이면 알림 상자만, 아니면 종전대로
    내 페이지 전체를 다시 그린다(자바스크립트 없이도 기능이 살아 있어야 한다).

    돌려주는 HTML은 전부 이 파일이 만든 **고정 문구**다(사용자 입력이 섞이지
    않는다) — 화면에서 그대로 심어도 안전한 이유가 여기에 있다.
    """
    if _wants_json(request):
        return JSONResponse({"notice_html": notice_html}, status_code=status_code)
    return None


async def mcp_test(request: Request) -> Response:
    """지금 이 주소가 실제로 응답하는지 서버가 대신 두드려 본다."""
    user_key = _session_user_key(request)
    if not user_key:
        return _test_result_response(request, _NOTICE_LOGIN_EXPIRED, 401) or HTMLResponse(
            _html_me_login_required(), status_code=401
        )

    with closing(identity.connect()) as conn:
        row = identity.get_by_user_key(conn, user_key)
        if row is None:
            return _test_result_response(
                request, _NOTICE_LOGIN_EXPIRED, 401
            ) or HTMLResponse(_html_me_login_required(), status_code=401)
        mcp_secret = (row or {}).get("mcp_secret")

    if not mcp_secret:
        notice = _NOTICE_NO_SECRET_TO_TEST
    else:
        # 장부 커넥션을 닫아 두고 두드린다 — 프로브는 최대 (5초 + 1초 + 5초)까지
        # 걸릴 수 있어, 그동안 SQLite 커넥션을 붙들고 있을 이유가 없다.
        #
        # run_in_threadpool이 필수다: 이 서버가 자기 자신에게 보내는 요청은 같은
        # 이벤트 루프가 받아 처리해야 하는데, 여기서 루프를 막아 버리면 그 요청이
        # 영원히 처리되지 않아 반드시 타임아웃한다(= 멀쩡한 주소가 늘 "확인 불가").
        verdict = await run_in_threadpool(probe_mcp_connection, mcp_secret)
        notice = _PROBE_NOTICES[verdict]

    json_response = _test_result_response(request, notice)
    if json_response is not None:
        return json_response

    with closing(identity.connect()) as conn:
        return _me_page_response(request, conn, user_key, notice)


async def mcp_rotate(request: Request) -> Response:
    """새 열쇠를 발급하고 새 주소를 그 자리에서 보여준다.

    옛 주소를 따로 막는 코드가 없는 것이 정상이다 — 라우팅 서버가 요청마다
    `identity.get_by_mcp_secret`으로 장부를 조회하므로, 장부 행이 바뀌는 순간
    옛 열쇠는 조회에 잡히지 않아 404가 된다(tests/test_web_auth.py가 이
    성질을 실제 앱으로 못 박는다).

    **쓰던 주소가 있으면 확인 화면을 한 번 거친다**(폐기와 같은 문턱). 그
    한 번이 곧 "AI에 등록해 둔 커넥터가 지금 끊긴다"는 뜻이기 때문이다.
    반대로 **폐기해 둔 상태에서 부르는 발급은 묻지 않는다** — 없던 주소를
    새로 만드는 것이라 끊길 연결 자체가 없고, 되돌리러 온 사람에게 문턱을
    세우면 그건 방해일 뿐이다.
    """
    user_key = _session_user_key(request)
    if not user_key:
        return HTMLResponse(_html_me_login_required(), status_code=401)

    form = await request.form()
    confirmed = (form.get("confirm") or "") == "yes"

    with closing(identity.connect()) as conn:
        row = identity.get_by_user_key(conn, user_key)
        if row is None:
            return HTMLResponse(_html_me_login_required(), status_code=401)
        if row.get("mcp_secret") and not confirmed:
            # 아직 아무것도 바꾸지 않았다 — 확인 화면만 보여준다.
            return HTMLResponse(_html_rotate_confirm())
        identity.rotate_mcp_secret(conn, user_key)
        logger.info("MCP 접속 열쇠 재발급 (user_key=%s)", user_key)  # 값 자체는 남기지 않는다
        return _me_page_response(request, conn, user_key, _NOTICE_ROTATED)


async def mcp_revoke(request: Request) -> Response:
    """열쇠를 없앤다. 실수로 한 번 누른 것만으로는 실행되지 않는다 — 확인
    화면을 한 번 거치고, 그 화면의 폼이 `confirm=yes`를 실어 다시 POST해야
    비로소 폐기한다."""
    user_key = _session_user_key(request)
    if not user_key:
        return HTMLResponse(_html_me_login_required(), status_code=401)

    form = await request.form()
    if (form.get("confirm") or "") != "yes":
        # 아직 아무것도 바꾸지 않았다 — 확인 화면만 보여준다.
        return HTMLResponse(_html_revoke_confirm())

    with closing(identity.connect()) as conn:
        if identity.get_by_user_key(conn, user_key) is None:
            return HTMLResponse(_html_me_login_required(), status_code=401)
        identity.revoke_mcp_secret(conn, user_key)
        logger.info("MCP 접속 열쇠 폐기 (user_key=%s)", user_key)
        return _me_page_response(request, conn, user_key, _NOTICE_REVOKED)


async def logout(request: Request) -> Response:
    """세션 쿠키를 지운다. path가 set_cookie와 어긋나면 지워지지 않으므로
    login/callback과 반드시 같은 `path="/auth"`를 쓴다."""
    resp = HTMLResponse(_html_logged_out())
    resp.delete_cookie(_SESSION_COOKIE_NAME, path="/auth")
    return resp


def build_auth_app() -> Starlette:
    """로그인 라우트 + 공개 페이지를 담은 Starlette 앱(순수 ASGI callable)을 만든다.

    lifespan 훅을 선언하지 않는다 — routing_server._AuthOrMcpDispatcher가
    lifespan scope를 이 앱으로 보내지 않는다(FastMCP 세션 매니저를 기동하는
    쪽은 MCP 앱 하나뿐이어야 한다).

    공개 페이지 목록을 여기 손으로 다시 적지 않고 `pages.PAGES`를 돌린다 —
    routing_server의 공개 경로 목록도 같은 곳(`ui.PUBLIC_PATHS`)을 보므로,
    메뉴를 하나 늘릴 때 한쪽만 늘어나 404가 나는 사고가 생기지 않는다.
    """
    public_routes = [
        Route(path, _public_page(path), methods=["GET"]) for path in pages.PAGES
    ]
    return Starlette(
        routes=public_routes
        + [
            Route("/auth/github/login", login, methods=["GET"]),
            Route("/auth/github/install", install, methods=["GET"]),
            Route("/auth/github/callback", callback, methods=["GET"]),
            # 2단계 — 저장소 마련하기. `/auth/repo/done`은 아무것도 바꾸지 않고
            # (이름을 담아 3단계로 넘길 뿐) 링크로 눌러도 안전하다.
            Route("/auth/repo", repo_step, methods=["GET"]),
            Route("/auth/repo/done", repo_done, methods=["GET"]),
            Route("/auth/github/select-repo", select_repo, methods=["GET"]),
            Route("/auth/me", me, methods=["GET"]),
            Route("/auth/memory", memory, methods=["GET"]),
            # 떼기는 되돌릴 수 없으므로 POST 전용 — 링크 프리페치·미리보기
            # 크롤러가 눌러 버리는 사고를 방법(method) 단계에서 막는다.
            Route("/auth/memo/remove", memo_remove, methods=["POST"]),
            # 주소 관리 3종은 POST만 받는다 — GET(링크·프리페치)으로는 절대
            # 실행되지 않아야 한다(파괴적 동작).
            Route("/auth/mcp/test", mcp_test, methods=["POST"]),
            Route("/auth/mcp/rotate", mcp_rotate, methods=["POST"]),
            Route("/auth/mcp/revoke", mcp_revoke, methods=["POST"]),
            Route("/auth/logout", logout, methods=["GET"]),
        ]
    )
