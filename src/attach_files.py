"""첨부 파일 주고받기 — 회원 GitHub 저장소의 `attach_file/`과 직접 오간다.

**서버 복제본을 거치지 않는다**(2026-08-06 사용자 확정 1). 복제본에 파일을 놓았다
지우면 그 삭제가 다음 push에 실려 GitHub에서도 파일이 사라지고, 지운 채로 두면
다음 갱신이 트리를 복원해 되살아난다 — 둘 다 실측으로 재현됐다. 그래서 파일은
GitHub Contents API로만 다루고, 이 서버 디스크에는 남기지 않는다.

이 모듈이 지키는 실측 사실 네 가지(2026-08-07):
1. **받기는 raw 형식으로 요청한다.** 기본 JSON 응답은 1 MiB를 넘으면 오류가 아니라
   빈 content를 조용히 준다 — 큰 파일이 말없이 0바이트가 된다.
2. **목록은 복제본이 아니라 API에 묻는다.** 복제본에 `git ls-tree -l`로 크기를
   물으면 git이 빠진 몸통을 전부 내려받아 첨부 격리가 뚫린다(파일 2,548개에 7분
   넘게 안 끝남). API 목록은 name·size만 주고 몸통은 안 준다.
3. **올린 뒤 최신화 표시를 지운다.** 안 지우면 서버 복제본이 그 커밋을 모른 채
   뒤처지고, 다음 기억 저장이 밀린 상태로 push를 시도한다.
4. **지워도 완전히 사라지지는 않는다.** git은 과거 이력을 남기므로 지우기 전
   시점으로 거슬러 올라가면 파일이 남아 있다. 도구 설명문이 이 사실을 알린다.
"""
from __future__ import annotations

import base64
import os
import posixpath
import re

import attach_text
import github_app
import user_repo

# 첨부가 들어가는 폴더. 나무 코어(config.ATTACH_DIR_NAME)·격리 설정과 같은
# 문자열이어야 한다 — 한 곳만 어긋나면 격리가 뚫린 채 파일이 모든 PC에 내려온다.
# user_repo가 이미 같은 이유로 같은 값을 들고 있어 그것을 재사용한다(두 벌로
# 늘리면 어긋날 자리가 하나 더 생긴다).
ATTACH_DIR_NAME = user_repo.ATTACH_DIR_NAME

# 파일 이름에 허용하지 않는 것 — 경로를 거슬러 올라가 저장소의 다른 폴더에 쓰는
# 것을 막는다. 첨부는 반드시 attach_file/ 안에만 놓인다.
_BAD_NAME_PARTS = ("..", "\\")

_MAX_BYTES_ENV = "NAMU_ATTACH_MAX_BYTES"
# 기본 상한 20 MiB — 2026-08-07 운영 서버에 실제 파일을 올려 재고 그대로 뒀다.
#
# 잰 값(올리기 링크로 보낸 시간, 서버가 스스로 잰 초):
#   1 MiB → 9.5초 · 5 MiB → 8.0초 · 20 MiB → 14.7초(그중 GitHub에 올리는 데 9.9초)
#   받기도 20 MiB가 15초에 내려왔고 바이트 하나까지 같았다.
# 티켓 경로는 파일 몸통이 AI의 출력을 거치지 않으므로 종전의 "6KB에 3분" 제약이
# 사라졌다 — 이제 시간을 정하는 것은 GitHub에 보내는 구간이고, 대략 크기에 비례한다.
#
# **이 위를 못 봤다는 것도 함께 적어 둔다.** 21 MiB를 보내면 이 상한이 먼저 413으로
# 거절하므로, GitHub Contents API가 몇 MiB까지 받는지는 확인하지 못했다. 상한을 올릴
# 일이 생기면 환경변수로 먼저 올려 재고 나서 이 숫자를 고칠 것(그러라고 환경변수로
# 뺐다). 올릴 때 볼 것 두 가지: 이 API는 몸통을 base64로 실어 보내 요청이 1.33배가
# 되고, 그 사본이 서버 메모리에 함께 올라간다.
_DEFAULT_MAX_BYTES = 20 * 1024 * 1024

# 글자 파일 판정은 **코어의 `attach_text`가 원본이다**(개인 주소와 이 서버가 같은
# 파일을 같게 판정해야 한다 — 한쪽이 원문으로, 다른 쪽이 base64로 다루면 같은
# 파일이 경로에 따라 달라진다). 여기서는 이름만 다시 내걸어 이 모듈을 쓰는 쪽이
# 두 군데를 뒤지지 않게 한다.
MAX_INLINE_TEXT_BYTES = attach_text.MAX_INLINE_TEXT_BYTES
TEXT_EXTENSIONS = attach_text.TEXT_EXTENSIONS
has_text_extension = attach_text.has_text_extension
as_text = attach_text.as_text


class AttachError(ValueError):
    """첨부 다루기 실패 — 사용자에게 그대로 보여줄 수 있는 메시지를 담는다."""


def max_bytes() -> int:
    """파일 하나의 크기 상한(바이트). 환경변수를 매 호출 읽는다.

    `user_repo.store_root()`와 같은 지연 평가다 — 모듈을 불러오는 시점에 굳히면
    테스트나 운영에서 값을 바꿔도 반영되지 않는다.
    """
    raw = os.environ.get(_MAX_BYTES_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise AttachError(
            f"{_MAX_BYTES_ENV} 값이 숫자가 아닙니다: {raw!r}. "
            f"{_MAX_BYTES_ENV} must be an integer (bytes)."
        ) from None
    if value <= 0:
        raise AttachError(
            f"{_MAX_BYTES_ENV}는 0보다 커야 합니다. {_MAX_BYTES_ENV} must be > 0."
        )
    return value


def normalize_name(name: str) -> str:
    """올리려는 이름을 `attach_file/<이름>` 한 칸으로 정규화한다.

    받아들이는 형태는 둘이다 — `보고서.pdf`와 `attach_file/보고서.pdf`. 앞에 폴더가
    붙어 있으면 그대로 쓰고, 없으면 붙인다. 어느 쪽이든 결과는 반드시
    `attach_file/` 아래 한 칸이며, 거슬러 올라가는 이름은 거절한다.
    """
    raw = (name or "").strip().strip("/")
    if not raw:
        raise AttachError("파일 이름이 비어 있습니다 — 올릴 이름을 적어 주세요.")
    if any(bad in raw for bad in _BAD_NAME_PARTS):
        raise AttachError(
            f"파일 이름에 쓸 수 없는 글자가 있습니다: {raw!r} — "
            "첨부는 attach_file/ 안에만 놓입니다."
        )
    prefix = f"{ATTACH_DIR_NAME}/"
    path = raw if raw.startswith(prefix) else prefix + raw
    # 정규화 후에도 폴더를 벗어나면 거절한다(예: attach_file/./../x).
    normalized = posixpath.normpath(path)
    if normalized != path or not normalized.startswith(prefix):
        raise AttachError(
            f"파일 경로가 첨부 폴더를 벗어납니다: {raw!r} — "
            "첨부는 attach_file/ 안에만 놓입니다."
        )
    if normalized.count("/") != 1:
        raise AttachError(
            f"첨부는 하위 폴더를 쓰지 않습니다: {raw!r} — "
            f"{prefix}<파일이름> 한 칸으로 적어 주세요."
        )
    return normalized


def _repo_and_token(conn, user_key: str) -> tuple[str, str]:
    """그 회원의 저장소 이름과 지금 쓸 수 있는 열쇠.

    미연결 회원은 `user_repo._require_connected`가 그대로 거절한다 — 판정을 여기
    베끼면 두 곳이 갈라진다.
    """
    record = user_repo._require_connected(conn, user_key)
    token = github_app.installation_token(record["installation_id"])
    return record["repo_full_name"], token


def _contents_url(repo_full_name: str, path: str) -> str:
    from urllib.parse import quote

    return (
        f"{github_app.GITHUB_API_BASE}/repos/{repo_full_name}"
        f"/contents/{quote(path)}"
    )


def _existing_sha(repo_full_name: str, path: str, token: str) -> str | None:
    """그 경로에 이미 파일이 있으면 그 sha, 없으면 None.

    덮어쓰기·지우기는 GitHub이 sha를 요구한다(다른 사람이 그 사이에 바꿨는지
    가리는 값이다). 없는 파일에 대해서는 404가 정상이므로 오류로 다루지 않는다.
    """
    import httpx

    resp = httpx.get(
        _contents_url(repo_full_name, path),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=github_app._ATTACH_HTTP_TIMEOUT_SEC,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise AttachError(
            f"저장소에서 파일 정보를 읽지 못했습니다 (status={resp.status_code}). "
            f"Could not read file metadata (status {resp.status_code})."
        )
    payload = resp.json()
    sha = payload.get("sha") if isinstance(payload, dict) else None
    return sha if isinstance(sha, str) else None


def upload(conn, user_key: str, name: str, content: bytes, message: str) -> dict:
    """파일 하나를 회원 저장소의 `attach_file/`에 올린다(있으면 덮어쓴다).

    돌려주는 것: `{"path", "bytes", "replaced"}`. `replaced`가 참이면 같은 이름이
    이미 있어 새 판으로 덮어쓴 것이며, 호출부가 첨부 기록의 상태를 '올림'이 아니라
    '새 판'으로 남기는 근거가 된다.
    """
    path = normalize_name(name)
    limit = max_bytes()
    if len(content) > limit:
        raise AttachError(
            f"파일이 너무 큽니다 ({len(content):,}바이트) — 지금 상한은 "
            f"{limit:,}바이트입니다. File too large."
        )
    if not content:
        raise AttachError("빈 파일은 올리지 않습니다 — 내용이 없습니다.")

    repo, token = _repo_and_token(conn, user_key)
    sha = _existing_sha(repo, path, token)
    body = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    github_app._put_json(_contents_url(repo, path), token, body)
    return {"path": path, "bytes": len(content), "replaced": bool(sha)}


def download(conn, user_key: str, name: str) -> bytes:
    """파일 하나를 받아 온다. 없는 파일은 그렇다고 알린다."""
    path = normalize_name(name)
    repo, token = _repo_and_token(conn, user_key)
    if _existing_sha(repo, path, token) is None:
        raise AttachError(
            f"저장소에 그런 파일이 없습니다: {path} — "
            "목록을 먼저 확인해 보세요. No such file in the repository."
        )
    return github_app._get_raw(_contents_url(repo, path), token)


def delete(conn, user_key: str, name: str, message: str) -> str:
    """파일 하나를 저장소에서 뺀다. 뺀 경로를 돌려준다.

    ⚠ 완전 삭제가 아니다 — git은 과거 이력을 남기므로 지우기 전 시점으로 거슬러
    올라가면 그 파일은 여전히 있다. 완전히 없애려면 저장소 이력을 다시 쓰는 별개
    작업이 필요하며 이 작업 범위 밖이다(2026-08-07 사용자에게 알린 사실).
    """
    path = normalize_name(name)
    repo, token = _repo_and_token(conn, user_key)
    sha = _existing_sha(repo, path, token)
    if sha is None:
        raise AttachError(
            f"저장소에 그런 파일이 없습니다: {path} — 이미 지워졌을 수 있습니다. "
            "No such file in the repository."
        )
    github_app._delete_json(
        _contents_url(repo, path), token, {"message": message, "sha": sha}
    )
    return path


def list_in_repo(conn, user_key: str) -> list[dict]:
    """저장소의 `attach_file/`에 **지금 실제로 있는** 파일들 — 이름과 크기.

    크기를 여기서 얻는 것이 안전한 이유는 모듈 docstring 2번 참고(API 목록은
    몸통을 안 준다). 폴더가 아직 없으면 빈 목록이다 — 오류가 아니라 "아직 아무것도
    안 올렸다"이다.
    """
    repo, token = _repo_and_token(conn, user_key)
    url = (
        f"{github_app.GITHUB_API_BASE}/repos/{repo}/contents/{ATTACH_DIR_NAME}"
    )
    rows = github_app._get_json_list(url, token)
    files = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "file":
            continue
        files.append(
            {
                "path": str(row.get("path") or ""),
                "bytes": row.get("size"),
            }
        )
    files.sort(key=lambda f: f["path"])
    return files


_SAFE_MESSAGE_RE = re.compile(r"[\r\n]+")


def commit_message(action: str, path: str) -> str:
    """커밋 한 줄. 줄바꿈을 지우는 이유는 커밋 메시지 첫 줄이 곧 제목이라,
    회원이 준 이름에 줄바꿈이 섞이면 이력이 엉뚱하게 잘려 보이기 때문이다."""
    return _SAFE_MESSAGE_RE.sub(" ", f"attach: {action} {path}").strip()
