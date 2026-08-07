"""GitHub App 인증 — 나무 서버가 "내가 그 앱이다"를 증명하고 installation
token(유효 1시간)을 받아오는 모듈 (사용자 신원 계층 1차).

이 서버는 사용자의 영구 자격증명(OAuth refresh token, PAT 등)을 보관하지
않는다. 각 사용자의 기억 원본은 사용자 본인의 GitHub repo에 있고, 나무는
사용자가 앱을 설치할 때 직접 고른 repo에 한해서만 유효한 **단명 토큰**으로
접근한다 — 서버가 털려도 새어 나갈 영구 열쇠 자체가 없게 만드는 것이 이
구조의 목적이다. 그래서 여기서 다루는 비밀은 두 가지뿐이다:
  1) 앱 개인키(PEM) — JWT 서명용. 환경변수로만 주입한다.
  2) installation token — 메모리 캐시에만 두고 디스크에 쓰지 않는다.

토큰 값은 로그·예외 메시지 어디에도 남기지 않는다(routing_server의
AuthMiddleware가 인증 실패 시 토큰 후보 값을 절대 로그에 남기지 않는 것과
같은 규칙). 실패 메시지에는 "무엇을 어떻게 설정해야 하는지"만 적는다.

환경변수는 전부 **호출 시점에 읽는다**(모듈 로드 시점 상수로 굳히지 않는다).
routing_server.store_root()와 같은 이유 — 테스트가 monkeypatch.setenv로
격리할 수 있어야 하고, 컨테이너 재기동 없이 값 교체가 가능해야 한다.
"""
import base64
import binascii
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt  # PyJWT — RS256 서명에 cryptography 백엔드 사용(pyjwt[crypto])

logger = logging.getLogger("namu.github_app")

GITHUB_API_BASE = "https://api.github.com"

# JWT 수명 상수 --------------------------------------------------------------
# GitHub는 app JWT의 exp - iat를 최대 10분으로 제한한다(초과 시 401).
_JWT_MAX_LIFETIME_SEC = 600
# 실제로 쓰는 수명은 상한보다 넉넉히 아래로 둔다(상한에 딱 붙이면 경계 판정이
# GitHub 쪽 반올림에 걸릴 수 있다).
_JWT_LIFETIME_SEC = 480
# iat를 현재 시각에서 60초 뒤로 미는 것은 GitHub 공식 권장이다 — 우리 호스트
# 시계가 GitHub보다 조금 빠르면 "미래에 발급된 토큰"으로 간주돼 거부되기 때문.
_JWT_CLOCK_SKEW_SEC = 60

# installation token 갱신 여유 ------------------------------------------------
# 만료 5분 전부터는 캐시를 믿지 않고 새로 발급받는다. 요청 처리 도중(clone/push
# 진행 중) 토큰이 만료돼 작업이 중간에 깨지는 것을 막기 위한 여유다.
_TOKEN_RENEW_MARGIN_SEC = 300
# expires_at을 못 읽었을 때 가정하는 보수적 수명(GitHub 실제 값은 1시간).
_TOKEN_FALLBACK_LIFETIME_SEC = 3000

_HTTP_TIMEOUT_SEC = 15.0

# 첨부 파일 주고받기는 메타데이터 호출과 달리 실제 파일이 오간다 — 15초로는
# 몇 MB짜리에서 쉽게 모자란다. 별도 상수로 둬서 이 값을 늘려도 토큰 발급 같은
# 짧아야 하는 호출의 타임아웃은 그대로 유지된다.
_ATTACH_HTTP_TIMEOUT_SEC = 120.0


# ---------------------------------------------------------------------------
# 환경변수 읽기 (전부 지연 평가) — 값 자체는 어떤 오류 메시지에도 넣지 않는다.
# ---------------------------------------------------------------------------
def _required_env(name: str, what: str) -> str:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise RuntimeError(
            f"{name} 환경변수가 설정되지 않았습니다 — {what} "
            f"Missing {name}: set it in the server environment (.env)."
        )
    return raw


def app_id() -> str:
    """GitHub App ID (JWT의 iss). 숫자지만 문자열로 다룬다 — GitHub 문서/화면
    표기를 그대로 옮겨 넣을 수 있게 하기 위함."""
    return _required_env(
        "NAMU_GITHUB_APP_ID",
        "GitHub App 설정 화면의 App ID를 넣으세요.",
    )


def app_private_key_pem() -> str:
    """앱 개인키 PEM.

    `NAMU_GITHUB_APP_PRIVATE_KEY`는 **base64로 인코딩된 PEM 한 줄**이다 —
    .env 파일 형식이 여러 줄 값을 담지 못해서(개행이 값의 끝으로 해석됨)
    base64로 접어 넣는다. 여기서 디코드해 원래 PEM으로 복원한다.

    비밀 관리자(Docker secret 등)가 개행 그대로 주입하는 배포도 있으므로,
    값이 이미 PEM이면(`-----BEGIN`으로 시작) 그대로 받아들인다.
    """
    raw = _required_env(
        "NAMU_GITHUB_APP_PRIVATE_KEY",
        "GitHub App 개인키(.pem)를 base64로 인코딩한 한 줄 문자열을 넣으세요 "
        "(예: base64 -w0 namu-memory-app.private-key.pem).",
    )
    if raw.startswith("-----BEGIN"):
        # _required_env가 strip()으로 걷어낸 끝 개행을 되돌린다. 이 스택
        # (cryptography/pyjwt)은 끝 개행이 없어도 정상 파싱하므로 지금은
        # 불필요하지만, 개행을 요구하는 다른 PEM 파서로 갈아탈 때를 대비한
        # 무해한 방어로 남긴다.
        return raw if raw.endswith("\n") else raw + "\n"
    # base64 값에 줄바꿈/공백이 섞여 들어오는 경우가 흔해 먼저 걷어낸다.
    compact = "".join(raw.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        # 예외 원문에 값 조각이 실릴 수 있으므로 체이닝하지 않고 새로 만든다.
        raise RuntimeError(
            "NAMU_GITHUB_APP_PRIVATE_KEY 값을 base64로 디코드하지 못했습니다 — "
            "개인키 .pem 파일을 base64로 인코딩한 한 줄 문자열인지 확인하세요. "
            "NAMU_GITHUB_APP_PRIVATE_KEY is not valid base64."
        ) from None
    pem = decoded.decode("utf-8", errors="replace")
    if "-----BEGIN" not in pem:
        raise RuntimeError(
            "NAMU_GITHUB_APP_PRIVATE_KEY를 디코드했지만 PEM 형식이 아닙니다 — "
            "GitHub App 개인키 .pem 파일 전체를 base64로 인코딩했는지 확인하세요. "
            "Decoded NAMU_GITHUB_APP_PRIVATE_KEY is not a PEM private key."
        )
    return pem


def client_id() -> str:
    """OAuth(사용자 로그인)용 Client ID — 2차 web_auth.py가 쓴다."""
    return _required_env(
        "NAMU_GITHUB_CLIENT_ID",
        "GitHub App 설정 화면의 Client ID를 넣으세요.",
    )


def client_secret() -> str:
    """OAuth(사용자 로그인)용 Client Secret — 2차 web_auth.py가 쓴다."""
    return _required_env(
        "NAMU_GITHUB_CLIENT_SECRET",
        "GitHub App 설정 화면에서 발급한 Client Secret을 넣으세요.",
    )


# ---------------------------------------------------------------------------
# app JWT
# ---------------------------------------------------------------------------
def app_jwt(now: datetime | None = None) -> str:
    """앱 개인키로 RS256 서명한 GitHub App JWT를 만든다.

    - iss: App ID
    - iat: 현재 시각 - 60초 (GitHub 권장 clock skew 보정)
    - exp: iat + 수명, 단 GitHub 상한(10분)을 넘지 않도록 잘라낸다.

    now는 테스트가 시각을 고정하기 위한 주입점이다(운영 경로에서는 미지정).
    """
    moment = now or datetime.now(timezone.utc)
    iat = int(moment.timestamp()) - _JWT_CLOCK_SKEW_SEC
    lifetime = min(_JWT_LIFETIME_SEC, _JWT_MAX_LIFETIME_SEC)
    payload = {"iat": iat, "exp": iat + lifetime, "iss": app_id()}
    return jwt.encode(payload, app_private_key_pem(), algorithm="RS256")


# ---------------------------------------------------------------------------
# HTTP — 네트워크 접점을 이 함수 하나로 모은다. 테스트는 이 함수만
# monkeypatch하면 네트워크 없이 토큰 발급/캐시 로직 전부를 검증할 수 있다.
# ---------------------------------------------------------------------------
def _post_json(url: str, token: str, body: dict | None = None) -> dict:
    """GitHub API에 POST하고 JSON 응답(dict)을 돌려준다.

    구현체로 httpx를 쓴다 — 타임아웃·JSON 처리가 표준 라이브러리보다 간결하다.
    지금은 mcp[cli]를 통해서도 런타임에 들어와 있지만, 우리가 직접 쓰는 이상
    전이 의존에 기대지 않고 pyproject.toml에 명시 선언했다(전이 의존은 상위
    패키지가 의존을 바꾸면 조용히 사라진다). import는 함수 안에서 한다
    (테스트가 이 함수를 통째로 갈아끼울 때 httpx를 굳이 로드하지 않게).

    실패 메시지에는 status/url만 남긴다 — Authorization 헤더 값(JWT)이나 응답
    본문은 절대 싣지 않는다.
    """
    import httpx

    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=body if body is not None else {},
        timeout=_HTTP_TIMEOUT_SEC,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"GitHub API 호출이 실패했습니다 (status={resp.status_code}, url={url}) — "
            "App ID/개인키/installation 설치 상태를 확인하세요. "
            f"GitHub API request failed with status {resp.status_code}."
        )
    return resp.json()


def _get_json(url: str, token: str) -> dict:
    """`_post_json`의 GET 버전 — 조회 전용 엔드포인트(예: 저장소 메타데이터)에
    쓴다. body가 없다는 점만 다르고 인증 헤더·타임아웃·오류 처리 방식은 동일하게
    맞춘다(namu-cloud-routing 3차 재검수 §3 — 사전 용량 관문이 저장소 크기를
    조회하려고 추가했다). httpx import를 함수 안에 두는 이유도 `_post_json`과
    같다(테스트가 이 함수를 통째로 갈아끼울 때 httpx를 굳이 로드하지 않게).
    """
    import httpx

    resp = httpx.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_HTTP_TIMEOUT_SEC,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"GitHub API 호출이 실패했습니다 (status={resp.status_code}, url={url}) — "
            "App ID/개인키/installation 설치 상태를 확인하세요. "
            f"GitHub API request failed with status {resp.status_code}."
        )
    return resp.json()


def _put_json(url: str, token: str, body: dict) -> dict:
    """`_post_json`의 PUT 버전 — 파일 하나 올리기(Contents API)에 쓴다.

    파일 첨부(namu-file-upload-download)는 서버 복제본을 거치지 않고 GitHub과
    직접 주고받는다. 복제본에 파일을 놓았다가 지우면 그 삭제가 push에 실려
    **GitHub에서도 파일이 사라지는** 것이 실측으로 확인됐기 때문이다.
    """
    import httpx

    resp = httpx.put(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=body,
        timeout=_ATTACH_HTTP_TIMEOUT_SEC,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"GitHub API 호출이 실패했습니다 (status={resp.status_code}, url={url}) — "
            f"GitHub API request failed with status {resp.status_code}."
        )
    return resp.json()


def _delete_json(url: str, token: str, body: dict) -> dict:
    """`_put_json`의 DELETE 버전 — 파일 하나 지우기(Contents API).

    httpx는 DELETE에 body를 실으려면 `request()`를 써야 한다(`delete()`는 json
    인자를 받지 않는다). GitHub Contents API의 삭제는 sha와 커밋 메시지를 body로
    요구하므로 이 형태가 강제된다.
    """
    import httpx

    resp = httpx.request(
        "DELETE",
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=body,
        timeout=_ATTACH_HTTP_TIMEOUT_SEC,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"GitHub API 호출이 실패했습니다 (status={resp.status_code}, url={url}) — "
            f"GitHub API request failed with status {resp.status_code}."
        )
    return resp.json()


def _get_raw(url: str, token: str) -> bytes:
    """파일 몸통을 **raw 형식으로** 받는다 — 첨부 받기 전용.

    기본 JSON 응답을 쓰면 안 되는 이유(2026-08-07 실측): Contents API는 파일이
    1 MiB(1,048,576바이트)를 넘으면 오류가 아니라 `encoding: "none"` + 빈 content를
    조용히 돌려준다. 그 상태를 "빈 파일"로 읽으면 큰 파일이 말없이 0바이트가 된다.
    `Accept: application/vnd.github.raw`로 요청하면 그 크기를 넘어도 전부 받아진다.
    """
    import httpx

    resp = httpx.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_ATTACH_HTTP_TIMEOUT_SEC,
        follow_redirects=True,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"GitHub API 호출이 실패했습니다 (status={resp.status_code}, url={url}) — "
            f"GitHub API request failed with status {resp.status_code}."
        )
    return resp.content


def _get_json_list(url: str, token: str) -> list:
    """폴더 안 목록(Contents API가 배열을 주는 경우)을 돌려준다.

    **크기를 물어도 파일 몸통은 안 내려온다**(2026-08-07 실측: 항목마다 name·size가
    오고 content는 비어 있다). 위험한 것은 서버 복제본에 `git ls-tree -l`로 묻는
    쪽이었다 — 그쪽은 크기를 알아내려고 빠진 몸통을 전부 내려받아 첨부 격리가
    뚫린다. 그래서 목록은 복제본이 아니라 이 API로 만든다.

    폴더가 아직 없으면 GitHub이 404를 준다 — 그건 오류가 아니라 "아직 아무것도
    안 올렸다"이므로 호출부가 빈 목록으로 다룰 수 있게 그대로 알린다.
    """
    import httpx

    resp = httpx.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_ATTACH_HTTP_TIMEOUT_SEC,
    )
    if resp.status_code == 404:
        return []
    if resp.status_code >= 400:
        raise RuntimeError(
            f"GitHub API 호출이 실패했습니다 (status={resp.status_code}, url={url}) — "
            f"GitHub API request failed with status {resp.status_code}."
        )
    payload = resp.json()
    return payload if isinstance(payload, list) else []


def repo_size_kb(repo_full_name: str, token: str) -> int:
    """`GET /repos/{owner}/{repo}`의 `size` 필드(KB 단위, 히스토리를 포함한
    저장소 전체 크기)를 installation token으로 조회한다.

    `user_repo.ensure_ready`의 clone 전 사전 용량 관문이 쓴다 — 왜 KB가 우리가
    실제로 받을 얕은 복제 크기를 과대평가하는지, 그 오차가 어느 방향으로
    안전한지는 `user_repo._PRECLONE_MAX_DECLARED_SIZE_BYTES` 주석 참고.

    `token`은 여기서 새로 발급받지 않고 호출부(`user_repo`)가 이미 들고 있는
    installation token을 그대로 받는다 — `_post_json`이 `token`을 인자로 받는
    기존 관례와 맞춘다(installation_token()이 자체적으로 app_jwt를 발급해
    호출하는 것과는 계층이 다르다: 그건 "누가 나무 서버인지" 증명하는 앱 JWT,
    이건 "그 설치에 한해 무엇을 볼 수 있는지"의 installation token).
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}"
    payload = _get_json(url, token)
    size = payload.get("size") if isinstance(payload, dict) else None
    if not isinstance(size, int) or isinstance(size, bool):
        raise RuntimeError(
            f"GitHub 저장소 메타데이터 응답에 size가 없습니다 (repo={repo_full_name}) — "
            "GitHub API 응답 형식이 바뀌었을 수 있습니다. "
            "GitHub repository metadata response did not contain a numeric size."
        )
    return size


# ---------------------------------------------------------------------------
# installation token 캐시 — 프로세스 메모리에만 둔다(디스크 기록 금지).
# ---------------------------------------------------------------------------
@dataclass
class _CachedToken:
    token: str
    expires_at: datetime


_TOKEN_CACHE: dict[int, _CachedToken] = {}
# 서버는 요청을 동시에 처리하므로 캐시 갱신을 락으로 감싼다. 락 안에서 HTTP를
# 하지는 않는다 — 발급이 느릴 때 다른 installation 요청까지 멈추지 않게.
_CACHE_LOCK = threading.Lock()


def clear_token_cache() -> None:
    """캐시 비우기 — 테스트 격리 및 운영 중 강제 재발급용."""
    with _CACHE_LOCK:
        _TOKEN_CACHE.clear()


def _validate_installation_id(installation_id: int) -> int:
    if isinstance(installation_id, bool) or not isinstance(installation_id, int):
        raise ValueError(
            "installation_id는 정수여야 합니다 — GitHub App 설치 시 부여된 숫자 ID입니다. "
            "installation_id must be an integer."
        )
    if installation_id <= 0:
        raise ValueError(
            "installation_id는 양수여야 합니다. installation_id must be positive."
        )
    return installation_id


def _parse_expires_at(raw, fallback: datetime) -> datetime:
    """GitHub의 `expires_at`("2026-07-26T12:00:00Z")을 aware datetime으로.

    파싱에 실패하면 보수적인 fallback(짧은 수명)을 쓴다 — 만료를 낙관해서
    죽은 토큰을 계속 재사용하는 것보다 한 번 더 발급받는 쪽이 안전하다.
    """
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        logger.warning("GitHub installation token의 expires_at 형식을 해석하지 못했습니다")
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def installation_token(installation_id: int) -> str:
    """installation_id에 해당하는 1시간짜리 access token을 돌려준다.

    캐시에 남은 토큰의 잔여 수명이 갱신 여유(5분)보다 크면 그대로 재사용하고,
    아니면 GitHub에 새로 발급받는다. installation_id별로 분리 보관한다 —
    한 사용자의 토큰이 다른 사용자 요청에 섞이면 곧바로 남의 repo 접근이 된다.
    """
    iid = _validate_installation_id(installation_id)
    now = datetime.now(timezone.utc)

    with _CACHE_LOCK:
        cached = _TOKEN_CACHE.get(iid)
    if cached is not None and cached.expires_at - now > timedelta(seconds=_TOKEN_RENEW_MARGIN_SEC):
        return cached.token

    url = f"{GITHUB_API_BASE}/app/installations/{iid}/access_tokens"
    payload = _post_json(url, app_jwt())
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        # 응답 본문을 그대로 싣지 않는다 — 토큰이 담겨 있을 수 있는 구조다.
        raise RuntimeError(
            f"GitHub installation token 응답에 token이 없습니다 (installation_id={iid}) — "
            "앱이 해당 계정에 설치돼 있는지 확인하세요. "
            "GitHub response did not contain an installation token."
        )
    expires_at = _parse_expires_at(
        payload.get("expires_at"),
        fallback=now + timedelta(seconds=_TOKEN_FALLBACK_LIFETIME_SEC),
    )
    with _CACHE_LOCK:
        _TOKEN_CACHE[iid] = _CachedToken(token=token, expires_at=expires_at)
    # 토큰 값은 남기지 않고 "언제까지 유효한지"만 기록한다.
    logger.info(
        "GitHub installation token 발급 (installation_id=%s, expires_at=%s)",
        iid,
        expires_at.isoformat(),
    )
    return token
