"""user_repo.py 유닛 테스트 — 실제 git으로 돌린다(요구사항: git 자체를 mock하지
않는다). `tmp_path`에 로컬 bare 저장소를 만들어 진짜 clone/fetch/push를 실행하고,
`github_app.installation_token`만 monkeypatch한다.

오프라인 대역이 필요한 지점은 `_authenticated_url`(우리 모듈의 내부 배선 함수)
하나뿐이다 — GitHub App은 github.com 전용이라 실제 네트워크 없이는 검증할 수
없으므로, "어느 저장소 주소를 가리킬지"만 로컬 bare 저장소로 바꿔치기한다. git
명령 자체(clone/fetch/push/reset의 실제 동작)는 전부 진짜로 실행된다.
"""
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import github_app
import identity
import routing_server as rs
import user_repo as ur


def _git(args: list[str], cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _connect_user(conn, github_id: int, login: str, repo_full_name: str = "owner/repo") -> str:
    key = identity.upsert_user(conn, github_id, login)
    identity.set_installation(conn, key, github_id, repo_full_name)
    return key


def _shift_last_seen(conn, user_key: str, days_ago: int) -> None:
    moment = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn.execute("UPDATE users SET last_seen_at = ? WHERE user_key = ?", (moment, user_key))
    conn.commit()


@pytest.fixture
def conn():
    c = identity.connect(":memory:")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _store_root(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path / "store"))
    return tmp_path / "store"


@pytest.fixture
def fake_token(monkeypatch):
    """github_app.installation_token / github_app.repo_size_kb를 대역으로 바꾼다
    — HTTP(GitHub API)를 실제로 타지 않는 것이 요구사항의 유일한 예외(git
    서브프로세스는 mock하지 않는다).

    `repo_size_kb`의 기본값(100KB)은 사전 용량 관문(§3, `_PRECLONE_MAX_DECLARED_SIZE_BYTES`
    500MB)에 전혀 걸리지 않는 작은 값이다 — 이 픽스처를 쓰는 대부분의 테스트는
    git clone/push 동작 자체를 검증하는 것이 목적이라, 사전 관문 기능을 직접
    겨냥하는 테스트(아래 "clone 전 사전 용량 관문" 절)만 이 값을 자체적으로
    다시 monkeypatch해 덮어쓴다.
    """
    monkeypatch.setattr(github_app, "installation_token", lambda installation_id: "FAKE_TEST_TOKEN")
    monkeypatch.setattr(github_app, "repo_size_kb", lambda repo_full_name, token: 100)
    return "FAKE_TEST_TOKEN"


@pytest.fixture
def bare_repo(tmp_path):
    """"사용자 GitHub 저장소" 역할을 하는 로컬 bare 저장소. 커밋 하나(a.txt)를 심어 둔다."""
    bare = tmp_path / "remote.git"
    _git(["init", "-q", "--bare", "-b", "main", str(bare)], cwd=tmp_path)
    seed = tmp_path / "_seed"
    _git(["clone", "-q", f"file://{bare}", str(seed)], cwd=tmp_path)
    _git(["config", "user.email", "seed@example.com"], cwd=seed)
    _git(["config", "user.name", "Seed"], cwd=seed)
    (seed / "a.txt").write_text("hello\n")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-q", "-m", "seed"], cwd=seed)
    _git(["push", "-q", "origin", "main"], cwd=seed)
    return bare


@pytest.fixture
def empty_bare_repo(tmp_path):
    """커밋이 0개인 진짜 빈 저장소 — GitHub에서 갓 만든 저장소의 기본값(실제 온보딩 시나리오)."""
    bare = tmp_path / "empty_remote.git"
    _git(["init", "-q", "--bare", "-b", "main", str(bare)], cwd=tmp_path)
    return bare


@pytest.fixture
def local_remote(monkeypatch, bare_repo):
    """`_authenticated_url`을 로컬 bare 저장소로 향하게 하는 유일한 대역 지점."""
    monkeypatch.setattr(ur, "_authenticated_url", lambda repo_full_name, token: f"file://{bare_repo}")
    return bare_repo


# ---------------------------------------------------------------------------
# user_dir / store_root — routing_server와의 계약
# ---------------------------------------------------------------------------
def test_user_key_regex_matches_routing_server():
    assert ur._USER_KEY_RE.pattern == rs._USER_KEY_RE.pattern


def test_store_root_matches_routing_server(tmp_path, monkeypatch):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    assert ur.store_root() == rs.store_root()


def test_user_dir_matches_routing_server_paths_for_user(tmp_path, monkeypatch):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    # DataPaths는 root를 직접 담지 않는다(learnings_yaml = root/memory/learnings.yaml) —
    # 조부모 디렉터리가 곧 라우팅 서버가 실제로 쓰는 사용자 폴더다.
    paths = rs._paths_for_user("alice")
    assert paths.learnings_yaml.parent.parent == ur.user_dir("alice")


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", "", "a b", "x" * 65, "a\x00b"])
def test_user_dir_rejects_bad_keys(bad):
    with pytest.raises(ValueError):
        ur.user_dir(bad)


# ---------------------------------------------------------------------------
# ensure_ready — 미연결 사용자 거부
# ---------------------------------------------------------------------------
def test_ensure_ready_rejects_uninstalled_user(conn):
    key = identity.upsert_user(conn, 100, "noapp")  # installation_id/repo 미설정
    with pytest.raises(ur.RepoNotConnected):
        ur.ensure_ready(conn, key)


def test_ensure_ready_rejects_unknown_user(conn):
    with pytest.raises(ur.RepoNotConnected):
        ur.ensure_ready(conn, "gh-999999")


# ---------------------------------------------------------------------------
# ensure_ready — clone/fetch, 얕음 유지 (뮤테이션 타깃 1: --depth 1 제거)
# ---------------------------------------------------------------------------
def test_ensure_ready_clones_shallow_and_scrubs_origin(conn, fake_token, local_remote):
    key = _connect_user(conn, 1, "alice")
    target = ur.ensure_ready(conn, key)

    assert target == ur.user_dir(key)
    assert (target / "a.txt").read_text() == "hello\n"
    assert (target / ".git" / "shallow").exists(), "clone이 얕게(depth 1) 유지되지 않았다"

    cfg_text = (target / ".git" / "config").read_text()
    assert "[remote" not in cfg_text, "origin 원격이 즉시 제거되지 않았다"
    assert "FAKE_TEST_TOKEN" not in cfg_text
    assert "x-access-token" not in cfg_text


def test_ensure_ready_stays_shallow_after_remote_advances(conn, fake_token, local_remote, bare_repo, tmp_path):
    key = _connect_user(conn, 2, "bob")
    ur.ensure_ready(conn, key)

    # 다른 PC가 먼저 커밋을 하나 더 쌓아 원격을 앞으로 진행시킨 상황을 흉내낸다.
    seed2 = tmp_path / "_seed2"
    _git(["clone", "-q", f"file://{bare_repo}", str(seed2)], cwd=tmp_path)
    _git(["config", "user.email", "s2@example.com"], cwd=seed2)
    _git(["config", "user.name", "S2"], cwd=seed2)
    (seed2 / "b.txt").write_text("world\n")
    _git(["add", "-A"], cwd=seed2)
    _git(["commit", "-q", "-m", "second"], cwd=seed2)
    _git(["push", "-q", "origin", "main"], cwd=seed2)

    target = ur.ensure_ready(conn, key)
    assert (target / ".git" / "shallow").exists(), "재동기화 후에도 얕은 상태여야 한다"
    assert (target / "b.txt").read_text() == "world\n"
    log_lines = _git(["log", "--oneline"], cwd=target).strip().splitlines()
    assert len(log_lines) == 1, "얕은 상태가 아니라 히스토리가 누적됐다 — depth 1이 깨졌다"


# ---------------------------------------------------------------------------
# ensure_ready — 빈 저장소(실제 온보딩 기본값) 실측 대응
# ---------------------------------------------------------------------------
def test_ensure_ready_handles_freshly_created_empty_repo(conn, fake_token, monkeypatch, empty_bare_repo):
    monkeypatch.setattr(ur, "_authenticated_url", lambda repo, token: f"file://{empty_bare_repo}")
    key = _connect_user(conn, 3, "carol")

    target = ur.ensure_ready(conn, key)

    assert target.exists() and (target / ".git").is_dir()
    result = subprocess.run(["git", "log"], cwd=target, capture_output=True, text=True)
    assert result.returncode != 0, "빈 저장소인데 커밋 로그가 존재한다"
    assert "does not have any commits yet" in (result.stdout + result.stderr)


def test_ensure_ready_second_call_on_still_empty_repo_does_not_raise(
    conn, fake_token, monkeypatch, empty_bare_repo
):
    monkeypatch.setattr(ur, "_authenticated_url", lambda repo, token: f"file://{empty_bare_repo}")
    key = _connect_user(conn, 4, "dave")
    ur.ensure_ready(conn, key)

    # 원격이 여전히 빈 채로 두 번째 호출 — "couldn't find remote ref HEAD"를 정상 처리해야 한다.
    target = ur.ensure_ready(conn, key)
    assert target.exists()


# ---------------------------------------------------------------------------
# push — 변경 없음/왕복/로컬 사본 없음
# ---------------------------------------------------------------------------
def test_push_with_no_changes_returns_false_and_does_nothing(conn, fake_token, local_remote):
    key = _connect_user(conn, 5, "erin")
    ur.ensure_ready(conn, key)
    assert ur.push(conn, key) is False


def test_push_round_trip_lands_on_user_repo(conn, fake_token, local_remote, bare_repo, tmp_path):
    key = _connect_user(conn, 6, "frank")
    target = ur.ensure_ready(conn, key)
    (target / "new.txt").write_text("added\n")

    assert ur.push(conn, key, message="add new.txt") is True

    check = tmp_path / "_check"
    _git(["clone", "-q", f"file://{bare_repo}", str(check)], cwd=tmp_path)
    assert (check / "new.txt").read_text() == "added\n"
    assert _git(["log", "-1", "--pretty=%s"], cwd=check).strip() == "add new.txt"


def test_push_without_ensure_ready_raises_local_copy_missing(conn, fake_token):
    key = _connect_user(conn, 7, "gina")
    with pytest.raises(ur.LocalCopyMissing):
        ur.push(conn, key)


def test_push_bootstraps_first_commit_from_empty_repo(
    conn, fake_token, monkeypatch, empty_bare_repo, tmp_path
):
    monkeypatch.setattr(ur, "_authenticated_url", lambda repo, token: f"file://{empty_bare_repo}")
    key = _connect_user(conn, 8, "henry")
    target = ur.ensure_ready(conn, key)
    (target / "first.txt").write_text("bootstrapped\n")

    assert ur.push(conn, key) is True

    check = tmp_path / "_check_empty"
    _git(["clone", "-q", f"file://{empty_bare_repo}", str(check)], cwd=tmp_path)
    assert (check / "first.txt").read_text() == "bootstrapped\n"


# ---------------------------------------------------------------------------
# push — non-fast-forward 거부, 강제 push 절대 금지 (뮤테이션 타깃 2: --force로 교체)
# ---------------------------------------------------------------------------
def test_push_rejects_non_fast_forward_without_overwriting_other_pc(
    conn, fake_token, local_remote, bare_repo, tmp_path
):
    key = _connect_user(conn, 9, "ivy")
    target = ur.ensure_ready(conn, key)

    # "다른 PC"가 먼저 push해서 원격이 앞서나간 상황.
    other = tmp_path / "_other_pc"
    _git(["clone", "-q", f"file://{bare_repo}", str(other)], cwd=tmp_path)
    _git(["config", "user.email", "o@example.com"], cwd=other)
    _git(["config", "user.name", "Other"], cwd=other)
    (other / "other.txt").write_text("from another pc\n")
    _git(["add", "-A"], cwd=other)
    _git(["commit", "-q", "-m", "other pc wrote first"], cwd=other)
    _git(["push", "-q", "origin", "main"], cwd=other)

    # 서버 사본(target)은 이를 모른 채 자기 변경을 만든다.
    (target / "mine.txt").write_text("from server copy\n")

    with pytest.raises(ur.PushRejected):
        ur.push(conn, key, message="server copy write")

    # 강제로 덮어쓰지 않았으므로 원격에는 "다른 PC"의 기록이 그대로 남아 있어야 한다.
    check = tmp_path / "_check_noforce"
    _git(["clone", "-q", f"file://{bare_repo}", str(check)], cwd=tmp_path)
    assert (check / "other.txt").exists(), "다른 PC의 기록이 사라졌다 — 강제 push 발생 의심"
    assert not (check / "mine.txt").exists(), "거부됐어야 할 push가 실제로 반영됐다"


def test_push_non_git_failure_raises_git_command_failed_not_push_rejected(
    conn, fake_token, local_remote, tmp_path, monkeypatch
):
    """마커가 하나도 안 맞는 실패(예: 아예 존재하지 않는 원격)는 `PushRejected`가
    아니라 `GitCommandFailed`로 나가야 한다 — 호출부가 두 예외에 다르게 반응할
    수 있으므로(non-fast-forward는 "다른 PC와 충돌", 그 외는 "그냥 실패") 이
    분기가 실제로 살아 있는지 실측으로 고정한다."""
    key = _connect_user(conn, 13, "mallory")
    target = ur.ensure_ready(conn, key)
    (target / "mine.txt").write_text("data\n")

    # push 시점에만 존재하지 않는 원격을 가리키게 해 non-fast-forward가 아닌
    # 실패(원격 저장소 자체가 없음)를 진짜 git으로 재현한다.
    monkeypatch.setattr(
        ur, "_authenticated_url", lambda repo_full_name, token: f"file://{tmp_path / 'does_not_exist.git'}"
    )

    with pytest.raises(ur.GitCommandFailed):
        ur.push(conn, key)


# ---------------------------------------------------------------------------
# non-fast-forward 마커 판정 — 순수 함수 `_is_non_fast_forward`를 마커별로
# 개별 겨냥한다(git을 mock하지 않는다는 원칙은 서브프로세스 실행에 관한 것이지,
# 이 텍스트 분류 함수의 순수 유닛 테스트와는 배치되지 않는다). 위
# `test_push_rejects_non_fast_forward_without_overwriting_other_pc`는 실제 git
# 2.43.0으로 non-fast-forward를 재현하는 e2e 테스트로 그대로 남겨둔다 — 아래
# 순수 유닛 테스트가 그것을 대체하지 않는다.
# ---------------------------------------------------------------------------
def test_is_non_fast_forward_matches_real_git_2_43_message():
    # 이 환경 git 2.43.0의 실제 거부 메시지(실측) — [rejected]와 fetch first가
    # 동시에 들어 있어 마커 하나만 지워서는 이 메시지 자체로는 회귀를 못 잡는다
    # (그래서 아래에 각 마커를 "단독으로" 겨냥하는 테스트를 따로 둔다).
    real_message = "! [rejected]        main -> main (fetch first)\n"
    assert ur._is_non_fast_forward(real_message)


def test_is_non_fast_forward_matches_rejected_marker_alone():
    # "[rejected]"만 있고 "fetch first"는 없는 경우 — stale-info류 다른 이유.
    text = "! [rejected]        main -> main (stale info)\n"
    assert ur._is_non_fast_forward(text)


def test_is_non_fast_forward_matches_fetch_first_marker_alone():
    # "fetch first"만 있고 "[rejected]" 대괄호 표기는 없는 가상의 변형 메시지.
    text = "remote rejected: please fetch first before pushing\n"
    assert ur._is_non_fast_forward(text)


def test_is_non_fast_forward_does_not_match_old_non_fast_forward_only_text():
    # "non-fast-forward"는 의도적으로 지운 마커다 — 그 문자열만 있고 나머지 두
    # 마커가 전혀 없는 가상의 구형 메시지는 이제 잡히지 않는 것이 의도된 동작임을
    # 문서화한다(모듈 docstring 근거 참고).
    text = "! [remote rejected] main -> main (non-fast-forward)\n".replace("[remote rejected]", "REASON:")
    assert not ur._is_non_fast_forward(text)


def test_is_non_fast_forward_no_marker_returns_false():
    assert not ur._is_non_fast_forward("some unrelated git error\n")


# ---------------------------------------------------------------------------
# clone 전 사전 용량 관문(§3, 3차 재검수) — GitHub `size`(KB)를 기준으로 clone
# 시작 자체를 막는다. 사후 검사(`_check_quota`)와 독립적으로 시험한다 — 한 겹이
# 다른 겹을 가리면 안 된다는 게 이번 재검수의 핵심 잣대였다.
# ---------------------------------------------------------------------------
def test_ensure_ready_refuses_clone_when_declared_size_too_large(
    conn, fake_token, local_remote, monkeypatch
):
    """선언 크기가 사전 상한을 넘으면 clone을 **시작조차 하지 않는다** — 폴더가
    아예 생기지 않아야 한다(post-check까지 갔다가 잡히는 것과는 다른 지점)."""
    # _PRECLONE_MAX_DECLARED_SIZE_BYTES는 리터럴로 monkeypatch(모듈 상수를 단언에
    # 참조하지 않는다는 규칙과 같은 이유로, 값 자체도 테스트 안에서 낮게 고정).
    monkeypatch.setattr(ur, "_PRECLONE_MAX_DECLARED_SIZE_BYTES", 1000)  # 1000 bytes
    monkeypatch.setattr(github_app, "repo_size_kb", lambda repo_full_name, token: 999999)  # 훨씬 큼
    key = _connect_user(conn, 14, "nancy")

    with pytest.raises(ur.QuotaExceeded):
        ur.ensure_ready(conn, key)

    assert not ur.user_dir(key).exists(), "사전 관문에서 걸렸는데도 clone 폴더가 생겼다"


def test_ensure_ready_allows_clone_when_declared_size_within_preclone_limit(
    conn, fake_token, local_remote, monkeypatch
):
    """선언 크기가 사전 상한 이내면 정상적으로 clone된다(사전 관문이 정상
    사용자까지 막지 않는지 확인)."""
    monkeypatch.setattr(ur, "_PRECLONE_MAX_DECLARED_SIZE_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(github_app, "repo_size_kb", lambda repo_full_name, token: 100)
    key = _connect_user(conn, 15, "oscar")

    target = ur.ensure_ready(conn, key)
    assert target.exists()
    assert (target / "a.txt").exists()


def test_ensure_ready_size_check_failure_blocks_clone(conn, fake_token, local_remote, monkeypatch):
    """크기 조회(GitHub API) 자체가 실패하면 fail-closed로 clone을 막는다 —
    "모르면 통과"가 아니라 "모르면 거부"다(근거는 `SizeCheckFailed` docstring)."""

    def _boom(repo_full_name, token):
        raise RuntimeError("simulated GitHub API outage")

    monkeypatch.setattr(github_app, "repo_size_kb", _boom)
    key = _connect_user(conn, 16, "peggy")

    with pytest.raises(ur.SizeCheckFailed):
        ur.ensure_ready(conn, key)

    assert not ur.user_dir(key).exists(), "크기 조회 실패인데도 clone 폴더가 생겼다"


def test_ensure_ready_precheck_passing_does_not_shadow_post_check(
    conn, fake_token, local_remote, monkeypatch
):
    """사전 관문(선언 크기)을 통과해도, clone 이후 실제 디스크 사용량 기준
    사후 검사(`_check_quota`)는 별도로 여전히 작동해야 한다 — 사전 관문이
    사후 검사를 가리면 안 된다(§2 fail 사유와 같은 유형의 문제).

    4차 재검수 §1(정리)이 붙은 뒤로는 사후 검사에서 거부된 clone 폴더가 곧바로
    지워지므로, "clone이 실제로 일어났다"를 폴더가 남아 있는 것으로는 더 이상
    확인할 수 없다 — 대신 `_cleanup_rejected_clone`이 지우기 **직전**에 폴더가
    실제로 존재했는지를 스파이로 잡는다.
    """
    monkeypatch.setattr(ur, "_PRECLONE_MAX_DECLARED_SIZE_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(github_app, "repo_size_kb", lambda repo_full_name, token: 1)  # 선언 크기는 작다
    monkeypatch.setattr(ur, "_MAX_USER_REPO_BYTES", 10)  # 그러나 실제 사후 상한은 매우 낮다
    key = _connect_user(conn, 17, "quentin")

    observed = {}
    real_cleanup = ur._cleanup_rejected_clone

    def _spy_cleanup(target):
        observed["existed_before_cleanup"] = target.exists() and (target / "a.txt").exists()
        real_cleanup(target)

    monkeypatch.setattr(ur, "_cleanup_rejected_clone", _spy_cleanup)

    with pytest.raises(ur.QuotaExceeded):
        ur.ensure_ready(conn, key)

    # 사전 관문은 통과했으므로 clone 자체는 실제로 일어났어야 한다(정리되기
    # 직전까지 폴더·파일이 실제로 존재했다).
    assert observed.get("existed_before_cleanup") is True, "사후 검사가 걸린 게 아니라 애초에 clone이 안 됐다"
    # 정리(§1)가 걸렸으므로 이 시점엔 폴더가 이미 지워져 있어야 한다.
    assert not ur.user_dir(key).exists()


# ---------------------------------------------------------------------------
# fetch 경로 사전 용량 관문(§2, 4차 재검수) — clone 경로와 대칭. clone 경로를
# 겨냥하는 위 테스트들이 이 방어를 대신 잡아주면 안 되므로, 이미 사본이 있는
# 사용자의 **두 번째** ensure_ready(fetch 분기)만을 겨냥한다(뮤테이션 타깃 8:
# fetch 분기의 `_check_declared_size_before_transfer` 호출 삭제).
# ---------------------------------------------------------------------------
def test_ensure_ready_refuses_fetch_when_declared_size_grows_too_large(
    conn, fake_token, local_remote, monkeypatch
):
    """최초 clone 시점엔 원격 선언 크기가 작아 통과했지만, 이후(다른 PC가 큰
    커밋을 push해) 원격이 커진 상태에서 두 번째 ensure_ready(fetch 경로)를
    부르면 fetch를 시작하기도 전에 거부해야 한다.

    사후 검사(`_check_quota`)는 실제 디스크 사용량 기준이라 이 시나리오(선언
    크기만 커졌을 뿐 로컬 사본은 여전히 작음)를 잡지 못한다 — 그래서 이 테스트가
    통과하려면 반드시 fetch 분기의 **사전** 관문이 살아 있어야 한다.
    """
    key = _connect_user(conn, 40, "rita")
    ur.ensure_ready(conn, key)  # 최초 clone — 사전 관문 통과(fake_token 기본 100KB)

    monkeypatch.setattr(ur, "_PRECLONE_MAX_DECLARED_SIZE_BYTES", 1000)  # 1000 bytes
    monkeypatch.setattr(github_app, "repo_size_kb", lambda repo_full_name, token: 999999)  # 원격이 커짐

    with pytest.raises(ur.QuotaExceeded):
        ur.ensure_ready(conn, key)


# ---------------------------------------------------------------------------
# 정리(cleanup) — 거부된 clone은 지우되 기존 사본은 절대 지우지 않는다
# (4차 재검수 §1). "새로 clone한 경우"와 "기존 사본"을 구분하는 것이 핵심이다.
# ---------------------------------------------------------------------------
def test_ensure_ready_over_quota_cleans_up_leftover_clone(conn, fake_token, local_remote, monkeypatch):
    """이번 호출에서 새로 clone했는데 사후 용량 검사에서 거부되면, 남겨진 clone
    폴더 자체가 정리돼야 한다(뮤테이션 타깃 9: `_cleanup_rejected_clone` 호출
    삭제) — 정리가 없으면 사전 관문(500MB)과 사후 상한(50MB) 차액이 실패할
    때마다 디스크에 눌러앉는다(§1 fail 사유)."""
    monkeypatch.setattr(ur, "_MAX_USER_REPO_BYTES", 10)
    key = _connect_user(conn, 41, "leaktest")

    with pytest.raises(ur.QuotaExceeded):
        ur.ensure_ready(conn, key)

    assert not ur.user_dir(key).exists(), "용량 초과로 거부된 clone 폴더가 정리되지 않고 남았다"


def test_ensure_ready_over_quota_on_fetch_path_preserves_existing_copy(
    conn, fake_token, local_remote, monkeypatch
):
    """이미 정상적으로 clone된 기존 사본이 있는 상태에서, 상한을 낮춰 fetch
    경로로 ensure_ready를 다시 부르면 예외는 나가되 **폴더와 그 안의 파일은
    그대로 남아 있어야 한다**(뮤테이션 타깃 10: 정리 범위를 "기존 사본까지"
    넓히면 이 테스트가 실패해야 한다) — 그 폴더에는 아직 사용자 GitHub으로
    push하지 못한 기록이 들어 있을 수 있어서, 지우면 그 기억이 사라진다."""
    key = _connect_user(conn, 42, "keepme")
    target = ur.ensure_ready(conn, key)
    assert (target / "a.txt").exists()

    monkeypatch.setattr(ur, "_MAX_USER_REPO_BYTES", 10)  # 사후 검사가 반드시 걸리게

    with pytest.raises(ur.QuotaExceeded):
        ur.ensure_ready(conn, key)

    assert target.exists(), "fetch 경로에서 거부됐는데 기존 사본 폴더가 지워졌다"
    assert (target / "a.txt").exists(), "기존 사본 안의 파일(아직 push 못했을 수 있는 기록)이 사라졌다"


def test_ensure_ready_cleanup_failure_does_not_mask_quota_exceeded(
    conn, fake_token, local_remote, monkeypatch, caplog
):
    """정리(rmtree) 자체가 실패해도 사용자에게 나가는 예외는 여전히
    `QuotaExceeded`여야 한다 — "정리 실패"가 사용자가 봐야 할 원래 원인("용량
    초과")을 가리면 안 된다. 다만 정리 실패 사실 자체가 완전히 사라지면
    운영자가 디스크가 왜 차는지 추적할 수 없으므로, 로그로는 남는지 함께
    확인한다."""
    monkeypatch.setattr(ur, "_MAX_USER_REPO_BYTES", 10)

    def _boom_rmtree(path):
        raise OSError("simulated rmtree failure")

    monkeypatch.setattr(ur.shutil, "rmtree", _boom_rmtree)
    key = _connect_user(conn, 43, "oswald")

    with caplog.at_level("ERROR", logger="namu.user_repo"):
        with pytest.raises(ur.QuotaExceeded):
            ur.ensure_ready(conn, key)

    assert any("정리" in rec.message for rec in caplog.records), "정리 실패가 로그로 전혀 남지 않았다"


# ---------------------------------------------------------------------------
# 50MB 상한 — 값 자체는 리터럴로 monkeypatch(단언에 모듈 상수를 참조하지 않는다)
# (뮤테이션 타깃 3: 용량 검사 삭제)
# ---------------------------------------------------------------------------
def test_ensure_ready_rejects_over_quota(conn, fake_token, local_remote, monkeypatch):
    monkeypatch.setattr(ur, "_MAX_USER_REPO_BYTES", 10)
    key = _connect_user(conn, 10, "jack")
    with pytest.raises(ur.QuotaExceeded):
        ur.ensure_ready(conn, key)


def test_push_rejects_over_quota_and_does_not_push(
    conn, fake_token, local_remote, bare_repo, tmp_path, monkeypatch
):
    key = _connect_user(conn, 11, "kate")
    target = ur.ensure_ready(conn, key)
    (target / "big.txt").write_text("x" * 5000)
    monkeypatch.setattr(ur, "_MAX_USER_REPO_BYTES", 100)

    with pytest.raises(ur.QuotaExceeded):
        ur.push(conn, key)

    check = tmp_path / "_check_quota"
    _git(["clone", "-q", f"file://{bare_repo}", str(check)], cwd=tmp_path)
    assert not (check / "big.txt").exists(), "용량 초과인데도 원격에 반영됐다"


def test_dir_size_reflects_real_file_growth(conn, fake_token, local_remote):
    key = _connect_user(conn, 12, "leo")
    target = ur.ensure_ready(conn, key)
    before = ur.dir_size(key)
    (target / "extra.bin").write_bytes(b"0" * 1000)
    after = ur.dir_size(key)
    assert after - before >= 1000


def test_dir_size_of_missing_dir_is_zero():
    assert ur.dir_size("never-cloned-user") == 0


# ---------------------------------------------------------------------------
# 토큰 마스킹 (뮤테이션 타깃 4: 마스킹 삭제)
# ---------------------------------------------------------------------------
def test_mask_token_pure_function():
    assert ur._mask_token("a SECRET b SECRET c", "SECRET") == "a *** b *** c"
    assert ur._mask_token("no token here", "SECRET") == "no token here"
    assert ur._mask_token("anything", None) == "anything"
    assert ur._mask_token("", "SECRET") == ""


def test_run_git_masks_token_in_failure_message(tmp_path):
    """실제 git 명령을 진짜로 실패시키고(mock 없음), 우리가 넘긴 인자 안에 든 토큰
    문자열이 예외 메시지에 새지 않는지 확인한다.

    이 시나리오가 실제 위험을 대표하는 이유: git이 인증된 URL을 인자로 받았다가
    실패하면 우리 쪽 진단 메시지 조립 코드가 그 인자를 그대로 포함시킬 수 있다
    (`subprocess.CalledProcessError`를 그대로 내보내면 `.cmd`에 토큰이 든 전체 인자가
    담긴다는 것을 별도로 실측 확인했다 — 모듈 docstring 참고). 여기서는 그 위험을
    낳는 실제 인자 형태(토큰이 든 URL)를 실제 git에 넘겨 실패시킨다.
    """
    token = "SECRET_TOKEN_ABC123"
    repo = tmp_path / "workdir"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    bad_url = f"file:///nonexistent/does/not/exist_{token}.git"
    with pytest.raises(ur.GitCommandFailed) as exc:
        ur._run_git(["fetch", "--depth", "1", "--no-tags", "--", bad_url, "HEAD"], cwd=repo, token=token)

    assert token not in str(exc.value), "토큰 마스킹이 안 돼 예외 메시지에 노출됐다"


# ---------------------------------------------------------------------------
# remove_stale — 조회는 identity, 삭제 실행은 이 모듈. 장부는 유지.
# ---------------------------------------------------------------------------
def test_remove_stale_deletes_only_stale_cached_copies(conn, fake_token, local_remote):
    stale_key = _connect_user(conn, 20, "stale-user")
    fresh_key = _connect_user(conn, 21, "fresh-user")
    ur.ensure_ready(conn, stale_key)
    ur.ensure_ready(conn, fresh_key)
    _shift_last_seen(conn, stale_key, days_ago=100)

    removed = ur.remove_stale(conn, days=30)

    assert removed == [stale_key]
    assert not ur.user_dir(stale_key).exists()
    assert ur.user_dir(fresh_key).exists()
    # 장부 행은 남아 있어야 한다 — 지우면 재가입처럼 보인다.
    assert identity.get_by_user_key(conn, stale_key) is not None


def test_remove_stale_missing_dir_is_silent_noop(conn):
    key = _connect_user(conn, 22, "never-cloned")
    _shift_last_seen(conn, key, days_ago=100)

    removed = ur.remove_stale(conn, days=30)  # ensure_ready를 호출한 적 없음

    assert removed == [], "폴더가 없는데도 지웠다고 보고했다"


# 뮤테이션 타깃 6: remove_stale이 stale_users 결과가 아니라 전체 사용자를 지우게 바뀌면 실패해야 함
def test_remove_stale_only_touches_stale_users_selection(conn, fake_token, local_remote, monkeypatch):
    key_a = _connect_user(conn, 23, "a")
    key_b = _connect_user(conn, 24, "b")
    ur.ensure_ready(conn, key_a)
    ur.ensure_ready(conn, key_b)

    # identity.stale_users 자체를 대역으로 바꿔 "고른 결과만" 지우는지 정확히 겨냥한다.
    monkeypatch.setattr(identity, "stale_users", lambda conn, days=30: [key_a])

    removed = ur.remove_stale(conn, days=30)

    assert removed == [key_a]
    assert not ur.user_dir(key_a).exists()
    assert ur.user_dir(key_b).exists(), "stale_users가 고르지 않은 사용자까지 지워졌다"


# 뮤테이션 타깃 5: remove_stale의 관문 (b)(resolve 후 users/ 하위 확인)를 지우면 실패해야 함
def test_remove_stale_refuses_symlink_escape_outside_users_root(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    users_root = tmp_path / "users"
    users_root.mkdir(parents=True)
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete")
    (outside / ".git").mkdir()  # 관문(c)까지는 통과시켜, 여기서는 관문(b)만 겨냥한다

    evil_key = "evilsymlink"
    (users_root / evil_key).symlink_to(outside, target_is_directory=True)

    identity.upsert_user(conn, 30, "victim")
    conn.execute("UPDATE users SET user_key = ? WHERE github_id = 30", (evil_key,))
    conn.commit()
    _shift_last_seen(conn, evil_key, days_ago=100)

    removed = ur.remove_stale(conn, days=30)

    assert evil_key not in removed
    assert outside.exists()
    assert (outside / "keep.txt").read_text() == "do not delete", "users/ 밖 폴더가 지워질 위험이 있었다"


# 뮤테이션 타깃 7(3차 재검수 §1): remove_stale의 관문 (c)(`.git` 존재 확인)를
# 지우면 실패해야 함. 관문 (a)(b)는 각각 test_user_dir_rejects_bad_keys /
# test_remove_stale_refuses_symlink_escape_outside_users_root가 전용으로 겨냥하는데
# (c)만 없었다 — 재현 대상은 "복제가 중간에 죽어 users/<키>/ 아래 .git 없이
# 반쯤 생긴 폴더"다.
def test_remove_stale_refuses_dir_without_git(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    users_root = tmp_path / "users"
    users_root.mkdir(parents=True)
    partial = users_root / "brokenclone"
    partial.mkdir()
    (partial / "leftover.txt").write_text("half-cloned, no .git here")

    key = "brokenclone"
    identity.upsert_user(conn, 31, "half-cloned-user")
    conn.execute("UPDATE users SET user_key = ? WHERE github_id = ?", (key, 31))
    conn.commit()
    _shift_last_seen(conn, key, days_ago=100)

    removed = ur.remove_stale(conn, days=30)

    assert key not in removed
    assert partial.exists(), "관문 (c) 없이 .git 없는 폴더까지 지워졌다"
    assert (partial / "leftover.txt").exists()
