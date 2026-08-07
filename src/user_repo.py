"""사용자 저장소 사본 관리 — 나무 미니PC가 들고 있는 "얕은 복제 캐시"의
준비·갱신·되돌려보내기·용량관리·정리 (사용자 신원 계층 3차).

설계 전제(namu-51 대시보드 설계 확정): 사용자 기억의 원본은 나무 서버가 아니라
**사용자 본인의 GitHub 저장소**다. 이 모듈이 `STORE_ROOT/users/<key>/`에 만드는
폴더는 순수 캐시라서 지워도 원본은 사용자 GitHub에 남는다 — 그래서 `remove_stale`이
장부(신원)는 그대로 두고 이 폴더만 지운다.

## 왜 `git clone`에 인증된 URL을 바로 못 넣는가 (핵심 판단)

작업 지시서는 "인증된 URL을 각 git 명령의 인자로만 넘긴다"고 했지만, 실측해 보면
`git clone <token-url> <dest>`는 그 URL을 **`.git/config`에 그대로 저장**한다
(`git remote -v`/`cat .git/config`로 실측 확인 — `[remote "origin"] url = https://x-access-token:<토큰>@...`
줄이 그대로 남는다). 이건 정확히 이 작업이 금지한 "`.git/config`에 토큰을 굽는" 상황이다.
`fetch`/`push`는 URL을 위치 인자로 직접 주면 어떤 remote 설정도 남기지 않지만(실측
확인), `clone`만 예외다.

그래서 clone 단계는 두 스텝으로 나눈다: ① `git clone <token-url> <dest>`로 받은 뒤
② 곧바로 `git remote remove origin`으로 방금 저장된 항목을 지운다. `git remote
set-url`(금지된 패턴 — 인증된 URL을 다시 심는 것)이 아니라 `remote remove`(설정을
아예 들어내는 것)를 쓰는 이유가 여기 있다 — 방향이 반대다. 이후 fetch/push는 항상
URL을 인자로 직접 넘기므로(원격 이름을 쓰지 않으므로) origin이 없어도 전혀 지장이
없다(실측 확인 — origin 없는 저장소에서도 URL 인자 fetch/push가 정상 동작).

## 왜 첨부 폴더(attach_file/)는 이 사본으로 안 내려오는가

파일 첨부(namu-file-upload-download)는 사용자 저장소에 `attach_file/` 폴더로 쌓이는데,
서버는 그 파일의 몸통을 읽을 일이 전혀 없다(파일은 GitHub API로 직접 오간다). 그런데
서버 사본은 사용자당 50MB 상한 안에서 살아야 하므로, 첨부가 여기까지 내려오면 상한을
곧바로 무너뜨린다. 그래서 이 사본은 **부분 복제(`--filter=blob:none`) + sparse-checkout
제외**로 첨부를 아예 받지 않는다. 자세한 배선 근거는 아래 "5) 첨부 격리" 절 주석에 있다.

## 왜 CalledProcessError를 밖으로 내보내지 않는가 (토큰 마스킹의 실제 근거)

이 git 버전은 인증 실패 시 자기 에러 메시지에서 이미 자격증명을 가려준다(실측:
`https://x-access-token:TOKEN@host/...`로 실패시켜도 `fatal: unable to access
'https://host/...'`처럼 자격증명 없이 보여준다). 반면 **파이썬
`subprocess.CalledProcessError`는 그렇지 않다** — `check=True`로 실행해 예외가
새 나가면 `str(exc)`/`exc.cmd`에 우리가 넘긴 인자(토큰이 박힌 URL)가 그대로 담긴다
(직접 재현: `subprocess.run([...token-url...], check=True)` 실패 시 `str(exc)`에
토큰이 그대로 노출됨). 그래서 `_run_git`은 **절대 `check=True`를 쓰지 않고**,
returncode를 직접 검사해 우리가 만든 메시지(항상 `_mask_token`을 통과한 것)만
예외에 싣는다. 명령 인자 자체도 진단용으로 메시지에 넣을 때는 마스킹을 거친다.
"""
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import github_app
import identity

logger = logging.getLogger("namu.user_repo")

# ---------------------------------------------------------------------------
# routing_server._validate_user_key / store_root 복제
# ---------------------------------------------------------------------------
# identity.py가 routing_server._USER_KEY_RE를 복제해 둔 것과 같은 이유·같은 관례다.
# routing_server는 이미 web_auth(→identity, github_app)를 얹고 있고, 2차 웹 라우트가
# routing_server에서 이 모듈(user_repo)을 호출하게 될 것이 확실시된다 — 그 시점에
# user_repo가 routing_server를 거꾸로 import하면 순환 참조가 된다. 두 정규식/경로
# 규칙이 어긋나면 tests/test_user_repo.py의 계약 테스트가 잡는다.
_USER_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_USER_KEY_ERROR_MSG = (
    "user_key 형식이 올바르지 않습니다(영숫자·하이픈·언더스코어 1~64자, 경로 이탈 금지). "
    "Invalid user_key format."
)


def _validate_user_key(key: str) -> str:
    if not isinstance(key, str) or not key or "\x00" in key or not _USER_KEY_RE.match(key):
        raise ValueError(_USER_KEY_ERROR_MSG)
    return key


def store_root() -> Path:
    """routing_server.store_root()의 복제 — 환경변수를 매 호출 시 읽는다(지연 평가
    원칙은 원본과 동일: 테스트가 monkeypatch.setenv로 격리할 수 있어야 한다)."""
    raw = os.environ.get("NAMU_STORE_ROOT", "").strip()
    if not raw:
        raise RuntimeError(
            "NAMU_STORE_ROOT 환경변수가 설정되지 않았습니다 — "
            "사용자 데이터가 쌓일 STORE clone 경로를 지정하세요."
        )
    return Path(raw)


def user_dir(user_key: str) -> Path:
    """`routing_server._paths_for_user`가 가리키는 것과 정확히 같은 폴더
    (`STORE_ROOT/users/<key>/`)를 돌려준다 — 라우팅 서버가 읽고 쓰는 기억 파일이 곧
    이 복제본 안의 파일이다.

    경로 탈출 이중 차단: ① 정규식으로 안전한 슬러그만 허용 ② resolve() 후
    `users/` 루트 밖으로 벗어나지 않는지 재확인. routing_server._paths_for_user와
    같은 방식이다(멀티테넌트 격리 방어선을 흩어지지 않게 그대로 미러).
    """
    _validate_user_key(user_key)
    users_root = (store_root() / "users").resolve()
    candidate = (users_root / user_key).resolve()
    try:
        candidate.relative_to(users_root)
    except ValueError:
        raise ValueError(_USER_KEY_ERROR_MSG) from None
    return candidate


# ---------------------------------------------------------------------------
# 예외 — 실패 종류별로 호출부가 다르게 반응해야 하므로 세분화한다.
# ---------------------------------------------------------------------------
class UserRepoError(RuntimeError):
    """이 모듈이 던지는 모든 예외의 공통 조상."""


class RepoNotConnected(UserRepoError):
    """GitHub App 미설치 또는 저장소 미연결 — 장부에 installation_id/repo_full_name이
    없다. 조용히 넘어가지 않고 온보딩 미완료를 명확히 알린다."""


class LocalCopyMissing(UserRepoError):
    """서버 사본 폴더가 아직 없다 — `ensure_ready()`를 먼저 호출해야 한다."""


class PushRejected(UserRepoError):
    """non-fast-forward — 서버 사본이 사용자 저장소보다 뒤처진 상태에서 push를
    시도했다. **강제로 덮어쓰지 않는다** — 사용자가 다른 PC에서 먼저 기록을 push한
    경우일 수 있고, 강제로 밀면 그 기억이 사라진다(이 작업의 핵심 안전 요구사항)."""


class QuotaExceeded(UserRepoError):
    """사용자당 상한(`_MAX_USER_REPO_BYTES`) 초과, 또는 clone 전 사전 관문
    (`_PRECLONE_MAX_DECLARED_SIZE_BYTES`)에서 GitHub가 알려준 저장소 선언 크기가
    이미 너무 큰 경우. 조용히 자르거나 지우지 않고 명시적으로 거부한다."""


class SizeCheckFailed(UserRepoError):
    """clone 전 저장소 크기를 GitHub API로 조회하려다 실패했다(네트워크 오류,
    GitHub 장애, 응답 형식 이상 등). 이 관문의 존재 이유가 디스크 고갈 방지이므로
    "모르면 통과"가 아니라 **fail-closed**로 clone을 막는다 — 조회 실패를 무시하고
    clone을 진행시키면, 하필 크기 조회가 실패하는 바로 그 순간에 사전 관문이
    무력화돼 이번에 막으려던 문제(거대한 clone이 다른 사용자 서비스를 멈추는 것)가
    그대로 재현된다. 사후 검사(`_check_quota`)가 있으니 안전하다는 반론은 성립하지
    않는다 — 사후 검사는 clone이 "끝난 뒤"에야 걸리므로, 디스크를 이미 다 쓴 뒤의
    사후 판정일 뿐 clone 도중의 디스크 고갈 자체는 막지 못한다(§3 요구사항이
    정확히 이 문제를 겨냥한다)."""


class GitCommandFailed(UserRepoError):
    """그 외 git 명령 실패. 메시지는 항상 `_mask_token`을 거친 상태(토큰 비노출)."""


# ---------------------------------------------------------------------------
# 토큰 취급 — 이 섹션을 통과하지 않은 문자열은 예외 메시지로 나가면 안 된다.
# ---------------------------------------------------------------------------
def _mask_token(text: str, token: "str | None") -> str:
    """`text` 안에 `token` 문자열이 있으면 전부 `***`로 바꾼다.

    git 명령 실행 결과(stdout/stderr)와, 진단용으로 메시지에 싣는 명령 인자
    설명(예: 실패한 git 하위명령과 그 인자)은 모두 이 함수를 거친 뒤에만 예외
    메시지에 들어간다 — `_run_git`이 유일한 통과 지점이다.
    """
    if not text or not token:
        return text
    return text.replace(token, "***")


def _authenticated_url(repo_full_name: str, token: str) -> str:
    """GitHub App installation token으로 인증된 HTTPS URL을 즉석에서 만든다.

    이 URL은 어떤 파일에도 저장하지 않는다 — 매 git 호출마다 새로 만들어 그
    호출의 인자로만 쓰고 버린다(§4 요구사항). GitHub App은 github.com 전용이라
    호스트를 하드코딩한다(엔터프라이즈 서버 지원은 이 작업 범위 밖).
    """
    return f"https://x-access-token:{token}@github.com/{repo_full_name}.git"


_GIT_TIMEOUT_SEC = 120.0


def _run_git(
    args: list[str],
    cwd: "Path | None" = None,
    token: "str | None" = None,
    timeout: float = _GIT_TIMEOUT_SEC,
) -> str:
    """git 서브프로세스를 실행하는 유일한 지점.

    `check=True`를 절대 쓰지 않는다(모듈 docstring의 "왜 CalledProcessError를
    밖으로 내보내지 않는가" 참고) — returncode를 직접 검사해, 우리가 만든
    (마스킹을 거친) 메시지만 예외에 싣는다. `args`(우리가 넘긴 명령 인자 — 토큰이
    박힌 URL을 포함할 수 있다)도 진단 문자열에 넣기 전에 마스킹한다.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        cmd_desc = _mask_token(" ".join(args), token)
        raise GitCommandFailed(f"git 명령이 시간 초과됐습니다: git {cmd_desc}") from None

    if proc.returncode != 0:
        cmd_desc = _mask_token(" ".join(args), token)
        output = _mask_token(f"{proc.stdout}{proc.stderr}".strip(), token)
        raise GitCommandFailed(
            f"git 명령 실패(exit={proc.returncode}): git {cmd_desc}\n{output}"
        )
    return proc.stdout


# git push가 non-fast-forward로 거부할 때 stderr에 남기는 마커 문자열.
#
# `non-fast-forward`는 이 목록에 없다 — 이 서버가 실제로 배포되는 환경(단일
# Docker 이미지, onnamu-project 미니PC)의 git 2.43.0을 실측하면 거부 메시지는
# 항상 `! [rejected]        main -> main (fetch first)` 형태이고, 이 문자열은
# 한 번도 나타나지 않는다(3차 재검수 실측 — 죽은 코드로 확인). 옛 git(2013년 이전,
# `(fetch first)`/`(stale info)`로 이유가 세분화되기 전 버전)이 `(non-fast-forward)`
# 하나로 뭉뚱그려 냈다는 기록은 있지만, 이 서버는 단일 통제된 배포 환경에서만
# 실행되고 사용자 PC의 git 버전과는 무관하다(git 명령은 항상 서버 컨테이너 안에서
# 실행된다) — 실제로 만날 일이 없는 마커를 "혹시 몰라서" 남겨두면 검증되지 않는
# 죽은 코드가 된다. `[rejected]`는 git의 push 상태 줄 형식(`send-pack` 프로토콜)에서
# 거부된 ref에 항상 붙는 접두어라(이유가 `fetch first`든 `stale info`든 무관하게)
# 이 마커 하나만으로도 non-fast-forward류 거부는 전부 걸린다 — `non-fast-forward`를
# 지워도 사각지대가 생기지 않는 근거다. 만약 나중에 실제로 관측되는 다른 git
# 버전에서 이 마커가 필요해지면, 그 관측을 증명하는 합성 문자열 유닛 테스트와
# 함께 다시 추가한다(추측만으로 추가하지 않는다).
_NON_FAST_FORWARD_MARKERS = ("[rejected]", "fetch first")


def _is_non_fast_forward(text: str) -> bool:
    """git push 실패 메시지(`text`)가 non-fast-forward 거부를 나타내는지 판정하는
    순수 함수. `push()`에서 분리한 이유: 판정 로직 자체를 실제 git 서브프로세스
    없이(문자열만 가지고) 마커별로 개별 겨냥해 테스트하기 위해서다(서브프로세스를
    mock하지 않는다는 이 프로젝트 원칙은 실행에 관한 것이지, 이 텍스트 분류
    함수의 순수 유닛 테스트와는 배치되지 않는다)."""
    return any(marker in text for marker in _NON_FAST_FORWARD_MARKERS)


# ---------------------------------------------------------------------------
# 장부 조회 — 앱 미설치/저장소 미연결을 여기서 한 번에 거른다.
# ---------------------------------------------------------------------------
def _require_connected(conn, user_key: str) -> dict:
    _validate_user_key(user_key)
    record = identity.get_by_user_key(conn, user_key)
    if record is None or not record.get("installation_id") or not record.get("repo_full_name"):
        raise RepoNotConnected(
            f"사용자({user_key})가 GitHub App을 설치하지 않았거나 저장소를 연결하지 "
            "않았습니다 — 온보딩(앱 설치 + 저장소 선택)을 먼저 완료해야 합니다. "
            "User has not installed the GitHub App or connected a repository yet."
        )
    return record


# ---------------------------------------------------------------------------
# 0) 첨부 격리 — `attach_file/`은 이 서버 사본으로 내려오지 않는다
#    (namu-file-upload-download 3단계. 파일 도구보다 **먼저** 깔려야 한다.)
# ---------------------------------------------------------------------------
# 폴더 이름은 나무 코어(vendor/namu-agent/namu-plugin/config.py의 ATTACH_DIR_NAME)와
# 같은 문자열이어야 한다 — 한쪽만 어긋나면 격리가 뚫린 채로 첨부가 서버에 쌓인다.
# 코어를 import하지 않고 복제해 두는 이유는 `_USER_KEY_RE`(routing_server 복제)와
# 같다: user_repo는 vendor를 sys.path에 얹지 않는 계층이다. 두 값이 어긋나면
# tests/test_user_repo.py의 계약 테스트가 잡는다.
ATTACH_DIR_NAME = "attach_file"

# sparse-checkout(부분 체크아웃) 패턴. `--no-cone` 형식이며 순서가 의미를 가진다 —
# 먼저 전부 포함하고 뒤에서 첨부 폴더만 뺀다.
_ATTACH_SPARSE_PATTERNS = ("/*", f"!/{ATTACH_DIR_NAME}/")

# promisor 원격(= "몸통이 빠져 있는 건 손상이 아니라 정상"이라는 표시)의 이름.
#
# ## 왜 이 표시가 필요한가 (실측 근거)
#
# 몸통이 빠진 사본에 promisor 원격이 하나도 없으면 `git gc`와 `git repack -ad`가
# `fatal: unable to read <oid>`로 **죽는다**(2026-08-07 실측). 서버 사본은 오래
# 살면서 계속 커밋이 쌓이는 폴더라 정리가 영영 안 되는 상태로 두면 안 된다.
#
# ## 왜 이 원격에 **주소를 안 적는가** (이게 이 설계의 핵심)
#
# 이 모듈의 절대 규칙은 "인증된 URL(토큰 포함)을 디스크에 남기지 않는다"이다.
# 그런데 `git fetch --filter=... -- <주소> HEAD`처럼 주소를 인자로 주면, git이
# 그 주소를 `[remote "<주소>"] promisor = true`로 **`.git/config`에 영구히 적어
# 넣는다**(2026-08-07 실측 — 인자 URL은 아무것도 안 남긴다는 이 모듈의 기존 전제가
# `--filter`를 붙이는 순간 깨진다). 그래서 필터를 쓰는 fetch는 반드시 **이름 붙은**
# 원격으로 해야 하고, 그 이름에는 주소를 적지 않는다.
#
# 주소는 매 호출마다 명령줄 설정(`-c remote.<이름>.url=...`)으로만 얹는다
# (`_promisor_url_args`). 명령줄 설정은 파일에 저장되지 않으므로 토큰이 디스크에
# 닿지 않는다. `remote.<이름>.url`은 값이 덮이는 자리가 아니라 **덧붙는 목록**이라,
# 파일에 주소가 하나라도 적혀 있으면 그쪽이 먼저 쓰인다(실측) — 그래서 "파일에는
# 주소를 아예 안 적는다"가 규칙이 된다.
#
# 부수 효과로 안전장치가 하나 더 생긴다: 주소 없이 몸통을 요청하면(예: 실수로
# 첨부 파일을 읽으려 하면) git이 조용히 내려받는 대신 그 자리에서 실패한다
# (`fatal: 'namu-origin' does not appear to be a git repository` — 실측). 서버가
# 첨부 몸통을 실수로도 받아올 수 없다는 뜻이다.
_PROMISOR_REMOTE_NAME = "namu-origin"


def _promisor_url_args(url: str) -> list[str]:
    """받아오는 명령(fetch/reset)에만 얹는 명령줄 설정 — 이 순간에만 존재하는 주소."""
    return ["-c", f"remote.{_PROMISOR_REMOTE_NAME}.url={url}"]


def attach_isolation_active(target: Path) -> bool:
    """이 사본에 첨부 격리가 이미 걸려 있는가(멱등 판정용).

    판정 근거는 promisor 표시(`remote.<이름>.partialclonefilter`) 하나다 — 이 값은
    우리가 격리를 위해 넣은 것이고, sparse-checkout(`core.sparseCheckout`)은 다른
    이유로도 켜질 수 있어 단독 근거가 되지 못한다.
    """
    if not (target / ".git").is_dir():
        return False
    proc = subprocess.run(
        ["git", "config", "--get", f"remote.{_PROMISOR_REMOTE_NAME}.partialclonefilter"],
        cwd=str(target),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "blob:none"


def _apply_sparse_exclusion(target: Path) -> None:
    """첨부 폴더가 작업트리에 나타나지 않게 한다(멱등).

    부분 복제(`--filter=blob:none`)만으로는 부족하다 — 필터는 "몸통을 미리 안
    받는다"일 뿐이라, 작업트리를 채우는 순간 빠진 몸통을 그때 받아온다. 작업트리에서
    빼야 비로소 받아올 이유 자체가 사라진다.
    """
    _run_git(
        ["sparse-checkout", "set", "--no-cone", *_ATTACH_SPARSE_PATTERNS], cwd=target
    )


def _mark_promisor_remote(target: Path) -> None:
    """"몸통이 빠져 있어도 정상"이라는 표시를 남긴다 — **주소는 적지 않는다**
    (이유는 `_PROMISOR_REMOTE_NAME` 주석). 이미 있으면 같은 값을 다시 쓸 뿐이다."""
    _run_git(["config", f"remote.{_PROMISOR_REMOTE_NAME}.promisor", "true"], cwd=target)
    _run_git(
        ["config", f"remote.{_PROMISOR_REMOTE_NAME}.partialclonefilter", "blob:none"],
        cwd=target,
    )


def _ensure_attach_isolation(target: Path) -> None:
    """이미 만들어져 있는 사본에 격리를 소급 적용한다(멱등).

    이 배선이 배포되기 전에 만들어진 서버 사본은 첨부를 걸러낼 설정이 하나도 없는
    통짜 복제다. 그런 사본도 다음 `ensure_ready` 때 이 함수를 통해 격리 상태로
    바뀌어야 한다 — 그러지 않으면 "새로 가입한 사람만 안전한" 반쪽 격리가 된다.
    """
    if attach_isolation_active(target):
        return
    _apply_sparse_exclusion(target)
    _mark_promisor_remote(target)


def _checkout_worktree(target: Path, token: "str | None") -> None:
    """`--no-checkout`으로 받아온 사본의 작업트리를 채운다.

    커밋이 하나도 없는 저장소(GitHub에서 갓 만든 기본값)에서는 건너뛴다 — 그
    상태에서 checkout은 `fatal: You are on a branch yet to be born`으로 실패하는데
    (실측), 이건 오류가 아니라 "채울 게 없다"는 정상 상태다.

    이 시점에는 아직 clone이 심어 둔 origin(토큰이 박힌 주소)이 살아 있어야 한다 —
    `--filter=blob:none`으로 몸통을 하나도 안 받았기 때문에, 포함 경로의 몸통은
    바로 이 checkout이 원격에서 끌어온다. 첨부는 sparse-checkout에서 이미 빠져
    있으므로 이때도 내려오지 않는다.
    """
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=str(target),
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode != 0:
        return
    _run_git(["checkout"], cwd=target, token=token)


# ---------------------------------------------------------------------------
# 1) 사본 준비/갱신
# ---------------------------------------------------------------------------
def ensure_ready(conn, user_key: str) -> Path:
    """사본을 "지금 쓸 수 있는 최신 상태"로 만든다. 없으면 얕게 clone, 있으면
    얕은 상태를 유지한 채(depth 1) fetch+hard reset으로 갱신한다.

    빈 저장소(커밋 0개, GitHub에서 갓 만든 저장소의 기본값) 대응: 최초 clone은
    `git clone`이 원격 HEAD의 심볼릭 링크(빈 저장소라도 존재)를 그대로 반영해
    커밋 없이도 올바른 기본 브랜치 이름으로 로컬을 만든다(실측 확인). 이미 있는
    로컬을 다시 fetch할 때는 원격이 여전히 비어 있으면 `couldn't find remote ref
    HEAD`로 실패하는데(실측 확인), 이건 오류가 아니라 "받아올 게 없다"는 정상
    상태이므로 조용히 넘어간다.
    """
    record = _require_connected(conn, user_key)
    target = user_dir(user_key)
    token = github_app.installation_token(record["installation_id"])
    url = _authenticated_url(record["repo_full_name"], token)

    # 이번 호출에서 새로 clone하는지(폴더가 아직 없었는지)를 시작 시점에 한 번만
    # 판정해 둔다 — 아래 사후 용량 검사가 실패했을 때 "이번에 새로 만든 clone만
    # 지운다"를 가르는 유일한 근거다(4번째 절 "정리" 참고). fetch 경로(else
    # 분기)로 들어왔다면 이 값은 항상 False이므로, 뒤에서 QuotaExceeded가 나도
    # 기존 사본은 절대 정리 대상이 되지 않는다.
    freshly_cloned = not (target / ".git").is_dir()

    if freshly_cloned:
        # §3 요구사항(3차 재검수) — clone을 시작하기 전에 GitHub가 알려주는
        # 저장소 크기부터 확인한다. 기존에는 clone을 통째로 끝낸 뒤에야
        # `_check_quota`로 걸렀는데, 그러면 거대한 저장소 하나가 clone "도중에"
        # 이미 미니PC 디스크(8GB, 나무 전체 예산 2GB)를 채워 다른 사용자 전원의
        # 서비스를 멈출 수 있다. 이 사전 관문은 그 상황 자체를 막는 값싼 1차
        # 방어선이고, `_check_quota`(clone 이후 실제 디스크 사용량 기준)는 여전히
        # 그대로 남아 최종 판정을 맡는다 — 두 겹은 서로 다른 시점·다른 근거로
        # 독립적으로 작동해야 한다(§2 fail 사유가 "여러 방어가 서로를 가림"이었던
        # 것과 같은 잣대).
        _check_declared_size_before_transfer(record, token, action="clone")
        target.parent.mkdir(parents=True, exist_ok=True)
        # `--filter=blob:none --no-checkout`: 파일 몸통을 하나도 안 받은 채로 뼈대만
        # 받는다. 첨부 격리를 **작업트리를 채우기 전에** 걸어야 하기 때문이다 —
        # 순서가 반대면 첨부가 한 번 내려온 뒤에 지우는 꼴이 되고, 그 사이 디스크를
        # 이미 다 쓴다.
        _run_git(
            [
                "clone", "--depth", "1", "--no-tags",
                "--filter=blob:none", "--no-checkout",
                "--", url, str(target),
            ],
            cwd=target.parent,
            token=token,
        )
        _apply_sparse_exclusion(target)
        _checkout_worktree(target, token=token)
        # clone은 인증된 URL을 .git/config에 origin으로 저장한다(실측 확인) — 토큰이
        # 디스크에 평문으로 남지 않도록 곧바로 지운다. 이후 fetch/push는 URL을 인자로
        # (또는 명령줄 설정으로) 직접 넘기므로 origin이 없어도 동작에 지장이 없다.
        _run_git(["remote", "remove", "origin"], cwd=target)
        # origin을 지우면 promisor 표시도 함께 사라진다 — 몸통이 빠진 사본에 그 표시가
        # 없으면 `git gc`/`repack`이 죽으므로(실측), 주소 없는 표시를 다시 남긴다.
        _mark_promisor_remote(target)
    else:
        # 4차 재검수 §2 — clone 경로와 대칭으로 갱신(fetch) 경로에도 같은 사전
        # 관문을 건다. 예전에는 이 분기에 사전 검사가 아예 없어서, 이미 연결된
        # 사용자의 원격이 나중에 커져도(예: 다른 PC에서 큰 파일을 커밋) 두 번째
        # 이후의 `ensure_ready` 호출이 무방비로 fetch를 시작했다.
        _check_declared_size_before_transfer(record, token, action="fetch")
        # 이 배선 이전에 만들어진 통짜 사본에도 여기서 격리를 소급 적용한다 —
        # 받아오기 **전에** 걸어야 이번 fetch부터 첨부 몸통이 안 들어온다.
        _ensure_attach_isolation(target)
        try:
            _run_git(
                [
                    *_promisor_url_args(url),
                    "fetch", "--depth", "1", "--no-tags", "--filter=blob:none",
                    _PROMISOR_REMOTE_NAME, "HEAD",
                ],
                cwd=target,
                token=token,
            )
        except GitCommandFailed as exc:
            if "couldn't find remote ref" in str(exc):
                # 원격이 여전히 커밋 0개인 빈 저장소다 — 받아올 게 없으므로 로컬
                # 상태(clone 시점에 만든, 커밋 없는 초기 브랜치)를 그대로 둔다.
                return target
            raise
        # reset에도 같은 주소를 얹는다. 걸러진 fetch는 바뀐 기억 파일의 몸통까지
        # 안 받아오기 때문에(실측), 작업트리를 갱신하는 이 순간에 그 몇 개만
        # 원격에서 끌어와야 한다 — 주소가 없으면 여기서 실패한다. 첨부는 작업트리에
        # 없으니 애초에 요청 대상이 아니다.
        _run_git(
            [*_promisor_url_args(url), "reset", "--hard", "FETCH_HEAD"],
            cwd=target,
            token=token,
        )

    try:
        _check_quota(user_key)
    except QuotaExceeded:
        # 4차 재검수 §1 — 이번 호출에서 새로 clone한 경우에만 정리한다. 기존
        # 사본(freshly_cloned=False, 즉 fetch 경로)은 여기로 와도 절대 지우지
        # 않는다 — 아직 사용자 GitHub으로 push하지 못한 기록이 그 안에 있을 수
        # 있어서다(모듈 상단 docstring의 설계 전제 참고). 원래의 QuotaExceeded는
        # 정리 성패와 무관하게 항상 그대로 다시 던진다.
        if freshly_cloned:
            _cleanup_rejected_clone(target)
        raise
    return target


def _cleanup_rejected_clone(target: Path) -> None:
    """사후 용량 검사(`_check_quota`)에서 거부된, **이번 호출에서 막 새로
    clone한** 폴더를 지운다. 호출부(`ensure_ready`)가 `freshly_cloned`일 때만
    불러서 기존 사본이 여기로 들어오지 않는 것을 보장한다 — 이 함수 자체는
    그 구분을 모른 채 받은 경로를 그대로 지운다.

    rmtree 자체가 실패해도 예외를 밖으로 던지지 않는다 — 호출부가
    `except QuotaExceeded:` 블록 안에서 이 함수를 부르므로, 여기서 새 예외가
    나가면 사용자에게 보여야 할 원래 원인("용량 초과")이 "정리 실패"로
    가려진다. 대신 정리 실패 자체를 완전히 숨기지는 않는다 — 그러면 운영자가
    디스크가 왜 차는지 추적할 방법이 없으므로 `logger.error`로 남긴다.
    """
    try:
        shutil.rmtree(target)
    except OSError:
        logger.error(
            "용량 초과로 거부된 clone 폴더 정리(rmtree)에 실패했습니다(%s) — "
            "디스크에 그대로 남아 있을 수 있으니 수동 확인이 필요합니다.",
            target,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# 2) 되돌려 보내기
# ---------------------------------------------------------------------------
DEFAULT_COMMIT_MESSAGE = "namu-cloud sync"
_COMMIT_AUTHOR_NAME = "namu-cloud-routing"
_COMMIT_AUTHOR_EMAIL = "namu-cloud-routing@users.noreply.github.com"


# ---------------------------------------------------------------------------
# 로컬 전용 캐시 파일 — 사용자 GitHub 저장소로 올라가면 안 되는 파일 (namu-58
# 4차 배선, 사용자 결정 4).
# ---------------------------------------------------------------------------
# db/namu.db는 memory/learnings.yaml·memory/profile.yaml에서 언제든 재생성 가능한
# 순수 검색 속도용 캐시다(routing_server._ensure_fresh가 stale이면 db.rebuild_from_yaml로
# 자동 재생성한다) — 원본이 아니므로 사용자 저장소에 실릴 이유가 없다. 이 모듈
# 상단 §3 주석의 실측(약 1.8MB)대로, 기존 `push()`가 그대로 `git add -A`만 했다면
# namu_record를 부를 때마다 1.8MB 이진 파일이 사용자 저장소 히스토리에 매번 새
# 커밋으로 쌓인다.
_LOCAL_ONLY_CACHE_RELATIVE_PATHS = ("db/namu.db",)


def _exclude_local_only_cache_paths(target: Path) -> None:
    """`_LOCAL_ONLY_CACHE_RELATIVE_PATHS`를 이 사본(로컬)의 `.git/info/exclude`에
    등록하고, **이미 추적(tracked) 중이라면** git 인덱스에서 빼낸다(작업 트리의
    실제 파일은 남긴다 — 로컬 검색 캐시로 계속 쓰이므로 지우면 안 된다).

    ## 왜 `.gitignore` 커밋이 아니라 `.git/info/exclude`인가

    `.gitignore`를 이 모듈이 사용자 저장소 안에 커밋해 넣으면, 우리가 임의로
    "사용자 저장소의 파일 목록"을 바꾸는 셈이 된다(§ 이 배선 작업의 원칙 —
    사용자 저장소를 우리 마음대로 오염시키지 않는다). `.git/info/exclude`는
    이 서버가 들고 있는 **로컬 사본 하나에만** 있는 git 설정 파일이라 원격
    저장소 내용에는 전혀 반영되지 않는다 — 커밋도 push도 되지 않는다(worktree가
    아니라 `.git/` 안이라는 점은 이 모듈의 다른 곳(예: `ensure_ready`가 clone
    직후 origin remote를 지우는 이유)과 같은 성격의 "로컬 전용 상태" 취급이다).

    ## 왜 "이미 추적 중인 경우"까지 다루는가

    `.git/info/exclude`(또는 `.gitignore`)는 git이 **아직 추적하지 않는** 파일
    에만 효과가 있다 — 이미 커밋된 파일은 제외 목록에 있어도 계속 추적된다.
    제외 목록만 믿고 "이제 빠지겠지"라고 가정하면, 과거에(예: 이 배선이 들어오기
    전 실수로, 혹은 사용자가 직접) 한 번이라도 커밋된 적 있는 db/namu.db는 이후
    영원히 계속 추적·커밋되는 결함이 남는다. `git rm --cached`로 인덱스에서만
    빼면(워킹트리 파일은 그대로 둔다 — `--cached` 없이 `git rm`을 쓰면 디스크
    파일까지 지워져 로컬 검색 캐시가 사라진다) 다음 커밋이 "그 파일을 저장소에서
    제거"하는 diff를 자연히 포함하게 되고, 이후로는 위 exclude 등록이 계속
    추적을 막아 준다.
    """
    exclude_path = target / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = (
        exclude_path.read_text(encoding="utf-8").splitlines()
        if exclude_path.exists()
        else []
    )
    missing = [p for p in _LOCAL_ONLY_CACHE_RELATIVE_PATHS if p not in existing_lines]
    if missing:
        with exclude_path.open("a", encoding="utf-8") as fh:
            for rel in missing:
                fh.write(rel + "\n")

    # git ls-files는 인자로 준 경로 중 "지금 인덱스에 실제로 있는" 것만 돌려준다
    # (아직 한 번도 커밋된 적 없으면 빈 문자열) — 그래서 매 push마다 불러도
    # 대부분의 호출에서는 아무 일도 하지 않는 값싼 확인이다.
    tracked = _run_git(
        ["ls-files", "--", *_LOCAL_ONLY_CACHE_RELATIVE_PATHS], cwd=target
    )
    for rel in tracked.splitlines():
        rel = rel.strip()
        if rel:
            _run_git(["rm", "--cached", "-q", "--", rel], cwd=target)


def push(conn, user_key: str, message: str = DEFAULT_COMMIT_MESSAGE) -> bool:
    """로컬 변경을 사용자 저장소로 되돌려 보낸다. 변경이 없으면 아무것도 하지
    않고 False.

    강제 push는 절대 하지 않는다 — non-fast-forward로 거부되면 `PushRejected`를
    던진다(§4 요구사항: 조용한 데이터 손실 금지). 실제 병합 정책은 이 작업
    범위 밖(후속 task)이다.

    `git add -A`를 하기 전에 반드시 `_exclude_local_only_cache_paths`를 먼저
    불러 db/namu.db(순수 캐시, §4 요구사항)가 이번 커밋에 실리지 않게 한다 —
    순서가 반대(add -A 먼저)면 이미 스테이징된 뒤에 제외 설정을 걸어봐야
    소용없다.
    """
    record = _require_connected(conn, user_key)
    target = user_dir(user_key)
    if not (target / ".git").is_dir():
        raise LocalCopyMissing(
            f"사용자({user_key})의 로컬 사본이 없습니다 — ensure_ready()를 먼저 "
            "호출하세요. Local copy is missing: call ensure_ready() first."
        )

    _exclude_local_only_cache_paths(target)

    status = _run_git(["status", "--porcelain"], cwd=target)
    if not status.strip():
        return False

    _run_git(["add", "-A"], cwd=target)
    _run_git(
        [
            "-c", f"user.name={_COMMIT_AUTHOR_NAME}",
            "-c", f"user.email={_COMMIT_AUTHOR_EMAIL}",
            "commit", "-q", "-m", message,
        ],
        cwd=target,
    )

    # push 직전 검사(§5 요구사항) — 방금 커밋한 변경까지 반영된 최종 크기로 판정한다.
    _check_quota(user_key)

    token = github_app.installation_token(record["installation_id"])
    url = _authenticated_url(record["repo_full_name"], token)
    branch = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=target).strip()

    try:
        _run_git(["push", "--", url, f"HEAD:{branch}"], cwd=target, token=token)
    except GitCommandFailed as exc:
        text = str(exc)
        if _is_non_fast_forward(text):
            raise PushRejected(
                "서버 사본이 사용자 저장소보다 뒤처져 push가 거부됐습니다"
                "(non-fast-forward) — 강제로 덮어쓰지 않았습니다. 사용자가 다른 PC에서 "
                "먼저 기록을 push했을 수 있습니다. "
                "Push rejected (non-fast-forward); refused to force-overwrite."
            ) from exc
        raise
    return True


# ---------------------------------------------------------------------------
# 3) 용량 관리 — 실측 3.5MB/사용자(얕은 복제 1.7MB + 검색 캐시 1.8MB)의 14배 여유.
# ---------------------------------------------------------------------------
_MAX_USER_REPO_BYTES = 50 * 1024 * 1024  # 50MB — 나무 전체 예산 2GB의 사용자당 상한선(사후 검사)

# clone 전 사전 관문(§3 요구사항, 3차 재검수) — `_check_quota`(사후 검사, 바로
# 아래)와는 독립된 별도 방어선이다. 임계값을 `_MAX_USER_REPO_BYTES`와 **같은
# 값으로 두지 않는다** — 근거:
#
# GitHub API의 `size`(KB)는 저장소의 **히스토리를 포함한 전체** 크기인데, 우리가
# 실제로 디스크에 받는 것은 `--depth 1` 얕은 복제뿐이다. 실측(같은 저장소 기준):
# 전체 clone 17MB vs 얕은 복제 1.7MB — GitHub `size`가 우리가 실제로 쓸 디스크
# 양을 **10배 과대평가**한다. 과대평가는 안전한 방향의 오차다(오탐이 있어도
# "정상 사용자를 거부"하는 쪽으로만 치우치지, "위험한 걸 통과"시키는 쪽으로는
# 치우치지 않는다) — 그래서 `_MAX_USER_REPO_BYTES`를 그대로 이 관문에 쓰면
# 실제로는 50MB 예산에 전혀 위협이 안 되는(얕은 복제 시 5MB 안팎일) 정상 저장소를
# 히스토리가 좀 크다는 이유만으로 clone 시작도 못 하고 거부하게 된다.
#
# 그렇다고 과대평가 배율(10배)을 그대로 믿고 `_MAX_USER_REPO_BYTES * 10`으로
# "정확히" 역산하지도 않는다 — 이 10배는 저장소 하나를 실측한 표본 하나일 뿐이고,
# 파일 히스토리 분포에 따라 얼마든지 달라진다(예: 큰 바이너리가 최신 커밋에만
# 있으면 얕은 복제도 전체 크기에 가까워진다). 이 관문의 역할은 "정확한 상한"이
# 아니라 "명백히 거대한 저장소의 clone 자체를 시작 전에 차단하는 값싼 1차
# 방어선"이므로, 사후 검사보다 넉넉한 별도 상수(`_MAX_USER_REPO_BYTES`의 10배,
# 500MB)로 둔다 — 실측 배율에 안전 여유를 더한 것이지 등식으로 맞춘 값이 아니다.
# 진짜 최종 판정(정확한 50MB 상한)은 clone 이후 `_check_quota`가 여전히 맡는다.
#
# 500MB는 낮추지 않는다(4차 재검수 사용자 결정) — 이 값을 좁히면 히스토리가
# 두꺼운 정상 사용자를 억울하게 거부하는 대가만 커진다. 역방향 위험도 있다는
# 점은 감춰서는 안 된다: 히스토리는 얇은데 HEAD 커밋 하나에 450MB짜리 단일
# 파일이 들어 있는 저장소는 `size`(KB, 전체 히스토리 기준)가 얕은 복제 실제
# 크기와 거의 같아져 10배 안전 여유가 사실상 없어지고, 그래도 이 500MB
# 상한선까지는 그대로 통과·수신된다. 이 위험을 감당 가능하게 만드는 것은
# 이 상수를 더 낮추는 것이 아니라 `ensure_ready`가 사후 `_check_quota`에서
# 거부한 clone을 **그 즉시 정리**(`_cleanup_rejected_clone`, 4차 재검수 §1)한다는
# 사실이다 — 그 정리가 없다면 이 500MB는 실패할 때마다 디스크에 영구히
# 눌러앉는 양이고, 정리가 있으면 clone 도중에만 잠깐 쓰는 일시적인 양이다.
# 8GB 미니PC에서 "일시적 500MB"는 감당 가능해도 "영구적 최대 450MB 누적"은
# 아니다 — 그래서 이 상수의 안전성은 `_cleanup_rejected_clone` 호출이 살아
# 있다는 전제에 의존한다. 나중에 누가 그 정리 로직을 지우면, 이 500MB
# 상한선의 근거도 함께 무너진다는 뜻이다.
_PRECLONE_MAX_DECLARED_SIZE_BYTES = _MAX_USER_REPO_BYTES * 10  # 500MB — 근거는 위 주석


def _check_declared_size_before_transfer(record: dict, token: str, action: str) -> None:
    """clone/fetch(둘 다 원격에서 받아오는 전송)를 시작하기 전에 GitHub 저장소
    선언 크기를 확인해, 명백히 너무 큰 저장소는 전송 자체를 시작하지 않고
    거부한다.

    4차 재검수 §2 전까지는 이 관문이 clone 경로(`ensure_ready`의 최초 연결)
    에만 있었다 — 이미 연결된 사용자의 원격이 나중에 부풀어도 갱신(fetch)
    경로는 무방비였다. 이제 두 경로가 이 함수 하나를 대칭으로 부른다.
    `action`("clone" 또는 "fetch")은 오류 메시지에만 쓰인다 — 판정 로직 자체는
    두 경로에서 동일하다.

    크기 조회(GitHub API) 자체가 실패하면 **fail-closed**로 막는다
    (`SizeCheckFailed` 참고 — 그 예외의 docstring에 근거를 자세히 적었다).
    """
    try:
        size_kb = github_app.repo_size_kb(record["repo_full_name"], token)
    except Exception as exc:
        raise SizeCheckFailed(
            f"저장소({record.get('repo_full_name')}) 크기를 {action} 전에 확인하지 "
            f"못했습니다 — 디스크 고갈 방지를 위해 {action}을(를) 시작하지 않고 "
            "거부합니다. 잠시 후 다시 시도하세요. "
            "Could not verify repository size before syncing; refusing to proceed."
        ) from exc

    size_bytes = size_kb * 1024
    if size_bytes > _PRECLONE_MAX_DECLARED_SIZE_BYTES:
        raise QuotaExceeded(
            f"저장소({record.get('repo_full_name')}) 선언 크기가 사전 상한을 초과해 "
            f"{action}을(를) 시작하지 않았습니다({size_kb}KB > "
            f"{_PRECLONE_MAX_DECLARED_SIZE_BYTES // (1024 * 1024)}MB). "
            f"Repository declared size exceeds the pre-transfer limit; refusing to {action}."
        )


def dir_size(user_key: str) -> int:
    """사용자 사본 폴더의 실제 디스크 사용량 합계(바이트). `.git` 포함 — 얕은
    복제라도 히스토리가 쌓이면 늘어나는 걸 놓치면 안 되기 때문에 전부 센다."""
    target = user_dir(user_key)
    if not target.exists():
        return 0
    total = 0
    for path in target.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
    return total


def _check_quota(user_key: str) -> None:
    size = dir_size(user_key)
    if size > _MAX_USER_REPO_BYTES:
        raise QuotaExceeded(
            f"사용자({user_key}) 서버 사본이 상한을 초과했습니다({size} bytes) — "
            "동기화를 거부합니다. 자동으로 파일을 지우거나 자르지 않습니다. "
            f"User repo copy exceeds the per-user quota ({size} bytes)."
        )


# ---------------------------------------------------------------------------
# 4) 정리 — 30일 미접속 사용자의 서버 사본만 지운다(장부는 유지).
# ---------------------------------------------------------------------------
def remove_stale(conn, days: int = 30) -> list[str]:
    """`identity.stale_users(conn, days)`가 고른 키의 서버 사본만 지운다.

    삭제 전 관문 3개를 모두 통과한 것만 rmtree한다:
      (a) 키 형식 검증 — `user_dir()`가 `_validate_user_key`로 수행
      (b) resolve() 후 `users/` 루트 하위인지 확인 — `user_dir()`가 수행
      (c) 그 폴더에 `.git`이 있는지 확인 — 여기서 별도로 확인

    장부 행은 지우지 않는다 — 다음 접속 시 `ensure_ready()`가 재복제하면 되고,
    신원 기록까지 지우면 재가입처럼 보인다(조회는 identity.stale_users 소관,
    삭제 실행은 이 함수 소관으로 분리해 실수로 전량 삭제되는 사고를 막는다).
    """
    removed: list[str] = []
    for key in identity.stale_users(conn, days=days):
        try:
            target = user_dir(key)  # (a)(b) 통과 못 하면 ValueError — 그 키는 건드리지 않는다
        except ValueError:
            continue
        if not target.exists():
            continue
        if not (target / ".git").is_dir():
            continue  # (c)
        shutil.rmtree(target)
        removed.append(key)
    return removed
