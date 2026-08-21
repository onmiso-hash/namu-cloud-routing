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
    # 부분 복제(`--filter=blob:none`)를 로컬 저장소 상대로도 실제로 겪게 한다.
    # 이 설정이 없으면 서버가 필터를 광고하지 않아 git이 "filtering not recognized
    # by server, ignoring"으로 조용히 통짜 전송으로 되돌아간다 — 그러면 첨부 격리
    # 테스트가 아무것도 검증하지 못한 채 통과한다(GitHub은 이 필터를 지원한다).
    _git(["config", "uploadpack.allowFilter", "true"], cwd=bare)
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
    _git(["config", "uploadpack.allowFilter", "true"], cwd=bare)
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
    assert '[remote "origin"]' not in cfg_text, "origin 원격이 즉시 제거되지 않았다"
    assert "FAKE_TEST_TOKEN" not in cfg_text
    assert "x-access-token" not in cfg_text
    # 첨부 격리 표시(`[remote "namu-origin"]`)는 남아 있어도 되지만, **주소 줄은
    # 어떤 형태로도 남으면 안 된다** — 주소가 남는다는 것은 곧 토큰이 디스크에
    # 적혔다는 뜻이기 때문이다(주소 없는 표시만 남기는 것이 이 설계의 핵심).
    assert "url = " not in cfg_text, f"원격 주소가 config에 남았다:\n{cfg_text}"


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
# push — db/namu.db(순수 검색 캐시)는 사용자 저장소로 절대 올라가지 않는다
# (namu-58 4차 배선, 사용자 결정 4). 진짜 git으로 실제 add/commit/push 결과를
# 확인한다(이 파일 전체 관례와 동일 — git 자체를 mock하지 않는다).
# ---------------------------------------------------------------------------
def test_exclude_local_only_cache_paths_is_idempotent(tmp_path):
    """순수 단위 테스트 — 실제 clone 없이 임시 저장소 하나에 두 번 불러도
    `.git/info/exclude`에 줄이 중복되지 않는지 확인한다."""
    target = tmp_path / "repo"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)

    ur._exclude_local_only_cache_paths(target)
    ur._exclude_local_only_cache_paths(target)

    lines = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert lines.count("db/namu.db") == 1, "exclude 등록이 중복됐다"


def test_push_writes_db_namu_db_into_git_info_exclude(conn, fake_token, local_remote):
    key = _connect_user(conn, 60, "excludecheck")
    target = ur.ensure_ready(conn, key)
    ur.push(conn, key)  # 변경이 없어도(False 반환) exclude 등록 자체는 이뤄져야 한다

    exclude_path = target / ".git" / "info" / "exclude"
    assert exclude_path.exists()
    assert "db/namu.db" in exclude_path.read_text(encoding="utf-8").splitlines()


def test_push_never_commits_db_namu_db_even_when_it_is_the_only_change(
    conn, fake_token, local_remote, bare_repo, tmp_path
):
    """db/namu.db만 새로 생겼을 때 — exclude가 status 단계부터 이 파일을 가려야
    하므로 push는 "변경 없음"(False)으로 끝나야 한다. 파일 자체(로컬 검색
    캐시)는 디스크에 그대로 남아야 하고, 원격에는 전혀 실리지 않아야 한다."""
    key = _connect_user(conn, 61, "cacheonly")
    target = ur.ensure_ready(conn, key)
    (target / "db").mkdir(parents=True, exist_ok=True)
    (target / "db" / "namu.db").write_bytes(b"FAKE-SQLITE-CACHE")

    assert ur.push(conn, key) is False

    assert (target / "db" / "namu.db").exists(), "로컬 캐시 파일 자체가 지워지면 안 된다"
    check = tmp_path / "_check_cache_only"
    _git(["clone", "-q", f"file://{bare_repo}", str(check)], cwd=tmp_path)
    assert not (check / "db").exists(), "캐시뿐인데도 원격에 db/ 폴더가 실렸다"


def test_push_excludes_db_namu_db_while_pushing_other_real_changes(
    conn, fake_token, local_remote, bare_repo, tmp_path
):
    """진짜 기억 파일 변경과 db/namu.db가 동시에 있을 때 — 진짜 변경만 커밋·push
    되고 캐시는 로컬에만 남아야 한다."""
    key = _connect_user(conn, 62, "cachewithreal")
    target = ur.ensure_ready(conn, key)
    (target / "db").mkdir(parents=True, exist_ok=True)
    (target / "db" / "namu.db").write_bytes(b"FAKE-SQLITE-CACHE")
    (target / "real.txt").write_text("real memory content\n")

    assert ur.push(conn, key, message="add real.txt") is True

    check = tmp_path / "_check_cache_with_real"
    _git(["clone", "-q", f"file://{bare_repo}", str(check)], cwd=tmp_path)
    assert (check / "real.txt").read_text() == "real memory content\n"
    assert not (check / "db").exists(), "캐시 파일이 진짜 변경과 함께 원격에 실렸다"
    assert (target / "db" / "namu.db").exists(), "로컬 캐시 파일이 사라졌다"


def test_push_untracks_previously_committed_db_namu_db(
    conn, fake_token, local_remote, bare_repo, tmp_path
):
    """이미 추적(tracked) 중인 경우 대응 — exclude 등록만으로는 이미 커밋된
    파일에 아무 효과가 없다(제외 목록은 "아직 추적하지 않는" 파일에만 통한다)는
    것이 이 테스트의 핵심 전제다. 과거(이 배선이 없던 시절)에 db/namu.db가
    실수로 커밋된 저장소를 흉내내고, 다음 push에서 실제로 인덱스에서 빠지는지
    (그러나 로컬 디스크 파일 자체는 남는지) 확인한다."""
    # "과거에 실수로 커밋된 db/namu.db"를 이 배선과 무관한 별도 clone으로 흉내낸다
    # (ur.push()를 거치지 않고 순수 git으로 직접 커밋 — 실제로 일어났을 법한
    # 과거 상태를 그대로 재현하기 위해서다).
    seed = tmp_path / "_seed_legacy"
    _git(["clone", "-q", f"file://{bare_repo}", str(seed)], cwd=tmp_path)
    _git(["config", "user.email", "legacy@example.com"], cwd=seed)
    _git(["config", "user.name", "Legacy"], cwd=seed)
    (seed / "db").mkdir()
    (seed / "db" / "namu.db").write_bytes(b"legacy cache blob")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-q", "-m", "legacy: accidentally committed db cache"], cwd=seed)
    _git(["push", "-q", "origin", "main"], cwd=seed)

    key = _connect_user(conn, 63, "legacyuser")
    target = ur.ensure_ready(conn, key)
    assert (target / "db" / "namu.db").read_bytes() == b"legacy cache blob"
    # clone 직후에는(우리 exclude 로직이 push() 시점에만 돌므로) 아직 추적 중이다.
    assert _git(["ls-files", "--", "db/namu.db"], cwd=target).strip() == "db/namu.db"

    (target / "real.txt").write_text("real change after legacy commit\n")
    assert ur.push(conn, key, message="untrack legacy cache") is True

    assert not _git(["ls-files", "--", "db/namu.db"], cwd=target).strip(), (
        "이미 추적 중이던 db/namu.db가 push 이후에도 여전히 추적되고 있다"
    )
    assert (target / "db" / "namu.db").exists(), (
        "인덱스에서만 빼야 하는데(--cached) 로컬 파일 자체가 지워졌다"
    )

    check = tmp_path / "_check_legacy"
    _git(["clone", "-q", f"file://{bare_repo}", str(check)], cwd=tmp_path)
    assert not (check / "db").exists(), "이미 추적 중이던 캐시 파일이 원격에서 빠지지 않았다"
    assert (check / "real.txt").read_text() == "real change after legacy commit\n"


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


# ---------------------------------------------------------------------------
# 첨부 격리 — `attach_file/`은 서버 사본으로 내려오지 않는다
# (namu-file-upload-download 3단계. 이 절의 테스트는 전부 진짜 git으로 돌린다.)
# ---------------------------------------------------------------------------
def _seed_attachment(bare, tmp_path, work_name: str, rel_path: str, size: int) -> None:
    """"다른 PC가 첨부 파일을 올렸다"를 흉내낸다 — bare 저장소에 실제 이진 파일을
    커밋해 넣는다. 크기가 있어야 "안 받아왔다"를 바이트로 확인할 수 있다."""
    work = tmp_path / work_name
    _git(["clone", "-q", f"file://{bare}", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "up@example.com"], cwd=work)
    _git(["config", "user.name", "Uploader"], cwd=work)
    path = work / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7f" * size)
    _git(["add", "-A"], cwd=work)
    _git(["commit", "-q", "-m", f"attach {rel_path}"], cwd=work)
    _git(["push", "-q", "origin", "main"], cwd=work)


def _blob_present(target, rev_path: str) -> bool:
    """그 경로의 파일 몸통이 이 사본의 손에 있는가. 없으면 git은 받아오려 시도했다가
    실패한다(주소를 안 적어 뒀으므로) — 그 실패가 곧 "안 받아왔다"의 증거다."""
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{rev_path}"],
        cwd=target, capture_output=True, text=True,
    )
    return proc.returncode == 0


def test_attach_dir_name_matches_core_config():
    """폴더 이름 계약 — 나무 코어(vendor)와 클라우드가 같은 문자열을 써야 한다.
    한쪽만 바뀌면 격리가 뚫린 채 첨부가 서버 사본에 쌓인다(되돌릴 수 없다)."""
    import routing_server  # noqa: F401 — vendor/namu-plugin을 sys.path에 얹는다
    import config as core_config

    assert ur.ATTACH_DIR_NAME == core_config.ATTACH_DIR_NAME


def test_ensure_ready_does_not_bring_attachment_bodies(conn, fake_token, local_remote, bare_repo, tmp_path):
    _seed_attachment(bare_repo, tmp_path, "_up1", "attach_file/big.bin", 400_000)
    key = _connect_user(conn, 40, "attach-clone")

    target = ur.ensure_ready(conn, key)

    assert (target / "a.txt").read_text() == "hello\n", "포함 경로는 정상적으로 있어야 한다"
    assert not (target / "attach_file").exists(), "첨부 폴더가 서버 작업트리에 나타났다"
    assert not _blob_present(target, "attach_file/big.bin"), "첨부 몸통이 서버로 내려왔다"
    assert ur.dir_size(key) < 200_000, f"400KB 첨부가 사본 크기에 반영됐다({ur.dir_size(key)}바이트)"


def test_ensure_ready_fetch_updates_memory_without_pulling_attachments(
    conn, fake_token, local_remote, bare_repo, tmp_path
):
    """갱신 경로의 핵심 — 바뀐 기억 파일은 반영되고, 같은 커밋에 함께 올라온 첨부는
    안 내려온다. (걸러진 fetch는 기억 파일 몸통조차 안 가져오므로, reset이 그 몇 개만
    끌어오는 배선이 살아 있어야 이 테스트가 통과한다.)"""
    key = _connect_user(conn, 41, "attach-fetch")
    target = ur.ensure_ready(conn, key)

    work = tmp_path / "_up2"
    _git(["clone", "-q", f"file://{bare_repo}", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "up@example.com"], cwd=work)
    _git(["config", "user.name", "Uploader"], cwd=work)
    (work / "a.txt").write_text("updated-from-other-pc\n")
    (work / "attach_file").mkdir()
    (work / "attach_file" / "later.bin").write_bytes(b"\x2f" * 400_000)
    _git(["add", "-A"], cwd=work)
    _git(["commit", "-q", "-m", "memory + attachment"], cwd=work)
    _git(["push", "-q", "origin", "main"], cwd=work)

    target = ur.ensure_ready(conn, key)

    assert (target / "a.txt").read_text() == "updated-from-other-pc\n", "기억 파일 갱신이 반영되지 않았다"
    assert not (target / "attach_file").exists()
    assert not _blob_present(target, "attach_file/later.bin"), "갱신 때 첨부 몸통이 내려왔다"
    assert ur.dir_size(key) < 200_000


def test_ensure_ready_never_writes_any_remote_url_to_config(
    conn, fake_token, local_remote, bare_repo, tmp_path
):
    """토큰 비노출의 최종 방어선 — clone/fetch 어느 경로를 지나도 `.git/config`에
    주소가 남으면 안 된다. `fetch --filter`를 주소 인자로 주면 git이 그 주소를
    `[remote "<주소>"]`로 영구히 적어 넣는다(실측) — 이 테스트가 그 회귀를 잡는다."""
    key = _connect_user(conn, 42, "no-url")
    target = ur.ensure_ready(conn, key)
    _seed_attachment(bare_repo, tmp_path, "_up3", "attach_file/x.bin", 1000)
    ur.ensure_ready(conn, key)  # 갱신(fetch) 경로까지 지난다

    cfg_text = (target / ".git" / "config").read_text()
    assert "url" not in cfg_text, f"원격 주소가 config에 남았다:\n{cfg_text}"
    assert "FAKE_TEST_TOKEN" not in cfg_text
    assert str(bare_repo) not in cfg_text


def test_git_gc_survives_on_isolated_copy(conn, fake_token, local_remote, bare_repo, tmp_path):
    """`git gc`가 죽지 않아야 한다 — 몸통이 빠진 사본에 promisor 표시가 없으면
    `fatal: unable to read <oid>`로 실패한다(실측). 서버 사본은 오래 사는 폴더라
    정리가 영영 안 되는 상태로 두면 안 된다."""
    _seed_attachment(bare_repo, tmp_path, "_up4", "attach_file/g.bin", 200_000)
    key = _connect_user(conn, 43, "gc-user")
    target = ur.ensure_ready(conn, key)

    proc = subprocess.run(["git", "gc"], cwd=target, capture_output=True, text=True)

    assert proc.returncode == 0, f"gc가 실패했다: {proc.stdout}{proc.stderr}"
    assert (target / "a.txt").read_text() == "hello\n"


def test_ensure_ready_retrofits_isolation_onto_existing_full_copy(
    conn, fake_token, local_remote, bare_repo, tmp_path
):
    """이 배선 이전에 만들어진 통짜 사본에도 격리가 소급 적용돼야 한다 —
    안 그러면 "새로 가입한 사람만 안전한" 반쪽 격리가 된다."""
    key = _connect_user(conn, 44, "legacy")
    _seed_attachment(bare_repo, tmp_path, "_up5", "attach_file/old.bin", 300_000)

    # 옛 코드가 만들던 그대로의 사본(통짜 얕은 복제 + origin 제거)을 손으로 만든다.
    target = ur.user_dir(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "-q", "--depth", "1", "--no-tags", f"file://{bare_repo}", str(target)], cwd=tmp_path)
    _git(["remote", "remove", "origin"], cwd=target)
    assert (target / "attach_file" / "old.bin").exists(), "사전 조건: 옛 사본에는 첨부가 있다"

    target = ur.ensure_ready(conn, key)

    assert ur.attach_isolation_active(target), "기존 사본에 격리가 적용되지 않았다"
    assert not (target / "attach_file").exists(), "소급 적용 후에도 첨부가 작업트리에 남아 있다"
    assert (target / "a.txt").read_text() == "hello\n"


def test_isolated_copy_still_pushes_and_keeps_attachments_on_user_repo(
    conn, fake_token, local_remote, bare_repo, tmp_path
):
    """격리는 서버가 안 받는 것일 뿐, 사용자 저장소의 첨부를 **지우면 절대 안 된다** —
    `git add -A`가 작업트리에 없는 제외 경로를 삭제로 스테이징하면 push 한 번에
    사용자의 첨부가 전부 사라진다."""
    _seed_attachment(bare_repo, tmp_path, "_up6", "attach_file/keep.bin", 50_000)
    key = _connect_user(conn, 45, "push-safe")
    target = ur.ensure_ready(conn, key)
    (target / "a.txt").write_text("server wrote this\n")

    assert ur.push(conn, key, message="server write") is True

    listed = _git(["ls-tree", "-r", "--name-only", "main"], cwd=bare_repo).split()
    assert "attach_file/keep.bin" in listed, "push가 사용자 저장소의 첨부를 지웠다"
    assert "a.txt" in listed
    assert _git(["show", "main:a.txt"], cwd=bare_repo) == "server wrote this\n"


def test_attach_isolation_is_idempotent_across_calls(conn, fake_token, local_remote, bare_repo, tmp_path):
    _seed_attachment(bare_repo, tmp_path, "_up7", "attach_file/i.bin", 1000)
    key = _connect_user(conn, 46, "idem")
    target = ur.ensure_ready(conn, key)
    first = (target / ".git" / "config").read_text()

    ur.ensure_ready(conn, key)
    ur.ensure_ready(conn, key)

    assert (target / ".git" / "config").read_text() == first, "반복 호출이 설정을 계속 바꾼다"
    assert ur.attach_isolation_active(target)
    assert not (target / "attach_file").exists()


# ---------------------------------------------------------------------------
# 죽은 git 잠금 치우기 (cloud-stale-git-lock)
# ---------------------------------------------------------------------------
def _age_file(path, seconds: float) -> None:
    """파일의 수정 시각을 `seconds`초 전으로 되돌린다 — 잠금의 '나이'를 만드는
    유일한 방법이다(청소 판정이 mtime만 본다)."""
    import os
    import time as _time

    moment = _time.time() - seconds
    os.utime(path, (moment, moment))


def _make_lock_fixture(root):
    """청소 대상이 섞여 있는 `.git` 한 벌 — 오래된 잠금 2개(하나는 하위 폴더),
    갓 생긴 잠금 1개, 잠금이 아닌 파일 1개."""
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    stale = root / ".git" / "index.lock"
    deep = root / ".git" / "refs" / "heads" / "main.lock"
    fresh = root / ".git" / "fresh.lock"
    plain = root / ".git" / "HEAD"
    for f in (stale, deep, fresh):
        f.write_text("")
    plain.write_text("ref: refs/heads/main\n")
    _age_file(stale, 3000)
    _age_file(deep, 3000)
    return root


def test_clear_stale_git_locks_matches_core_implementation(tmp_path):
    """잠금 청소 계약 — 나무 코어(vendor)와 이 방의 복제본이 같은 판정을 해야 한다.

    `ATTACH_DIR_NAME`과 같은 이유로 코어를 import하지 않고 복제했으므로(user_repo는
    vendor를 sys.path에 얹지 않는 계층), 한쪽만 바뀌었을 때 여기서 잡는다. 문자열
    비교가 아니라 **같은 폴더 두 벌에 각각 돌려 결과를 맞춰 보는** 방식이다 — 구현이
    달라도 판정이 같으면 계약은 지켜진 것이기 때문이다.
    """
    import routing_server  # noqa: F401 — vendor/namu-plugin을 sys.path에 얹는다
    import startup_sync as core_sync

    ours = _make_lock_fixture(tmp_path / "ours")
    theirs = _make_lock_fixture(tmp_path / "theirs")

    mine = ur.clear_stale_git_locks(ours, max_age_seconds=600)
    core = core_sync.clear_stale_git_locks(theirs, max_age_seconds=600)

    assert mine, "오래된 잠금을 하나도 안 지웠다면 이 검사는 아무것도 확인하지 못한다"
    assert mine == core, f"코어와 판정이 갈렸다: 클라우드={mine} 코어={core}"
    left_ours = sorted(p.name for p in (ours / ".git").rglob("*") if p.is_file())
    left_theirs = sorted(p.name for p in (theirs / ".git").rglob("*") if p.is_file())
    assert left_ours == left_theirs
    assert left_ours == ["HEAD", "fresh.lock"], "갓 생긴 잠금이나 잠금 아닌 파일까지 건드렸다"


def test_clear_stale_git_locks_ignores_missing_git_dir(tmp_path):
    """`.git`이 없는 폴더에서도 조용히 빈 목록 — 청소는 관문이 아니라 거들기다."""
    (tmp_path / "empty").mkdir()
    assert ur.clear_stale_git_locks(tmp_path / "empty") == []


def test_stale_lock_age_is_above_git_timeout():
    """5분 기준의 근거 — 살아 있는 우리 git이 쥔 잠금은 `_GIT_TIMEOUT_SEC`(120초)를
    넘길 수 없다. 기준을 그 아래로 내리면 살아 있는 잠금을 지우게 된다."""
    assert ur.STALE_LOCK_AGE_SECONDS > ur._GIT_TIMEOUT_SEC


def test_ensure_ready_clears_stale_lock(conn, fake_token, local_remote, bare_repo, tmp_path):
    """실제 사고 재현 — 앞 세대가 남긴 `index.lock`이 있으면 `reset --hard`가
    `File exists`로 죽어 그 사용자의 기억이 통째로 막힌다. 이제는 스스로 치운다."""
    key = _connect_user(conn, 70, "stale-lock")
    target = ur.ensure_ready(conn, key)

    lock = target / ".git" / "index.lock"
    lock.write_text("")
    _age_file(lock, 3000)

    again = ur.ensure_ready(conn, key)

    assert not lock.exists(), "죽은 잠금이 그대로 남았다"
    assert (again / "a.txt").read_text() == "hello\n", "청소 뒤 갱신이 정상으로 끝나야 한다"


def test_ensure_ready_keeps_fresh_lock(conn, fake_token, local_remote, bare_repo, tmp_path):
    """갓 생긴 잠금은 건드리지 않는다 — 같은 사용자의 다른 요청이 지금 git을 돌리고
    있을 수 있다. 그 잠금 때문에 이번 호출이 실패하는 것이 **맞는 동작**이다
    (남의 살아 있는 작업을 깨뜨리는 것보다 낫다)."""
    key = _connect_user(conn, 71, "fresh-lock")
    target = ur.ensure_ready(conn, key)

    lock = target / ".git" / "index.lock"
    lock.write_text("")

    with pytest.raises(ur.GitCommandFailed):
        ur.ensure_ready(conn, key)

    assert lock.exists(), "살아 있을 수 있는 잠금을 지웠다"


def test_push_clears_stale_lock(conn, fake_token, local_remote, bare_repo, tmp_path):
    """push 경로에도 같은 청소가 걸려 있어야 한다 — add/commit도 잠금을 잡는다."""
    key = _connect_user(conn, 72, "stale-lock-push")
    target = ur.ensure_ready(conn, key)
    (target / "new.txt").write_text("world\n")

    lock = target / ".git" / "index.lock"
    lock.write_text("")
    _age_file(lock, 3000)

    assert ur.push(conn, key, "잠금 청소 뒤 push") is True
    assert not lock.exists()


def test_clear_locks_at_startup_removes_even_fresh_locks(_store_root):
    """기동 청소는 나이를 안 본다 — 이 시점에는 우리 git 프로세스가 하나도 없어서
    남아 있는 잠금이 전부 앞 세대의 것이기 때문이다. 사용자 전원을 훑는다."""
    users = _store_root / "users"
    for name in ("gh-1", "gh-2"):
        (users / name / ".git" / "refs" / "heads").mkdir(parents=True)
        (users / name / ".git" / "index.lock").write_text("")
    deep = users / "gh-2" / ".git" / "refs" / "heads" / "main.lock"
    deep.write_text("")
    stray = users / "not-a-dir.txt"
    stray.write_text("사용자 폴더가 아닌 것은 건너뛴다")

    removed = ur.clear_locks_at_startup()

    assert sorted(removed) == [
        "users/gh-1/.git/index.lock",
        "users/gh-2/.git/index.lock",
        "users/gh-2/.git/refs/heads/main.lock",
    ]
    assert not deep.exists()
    assert stray.exists()


def test_clear_locks_at_startup_without_volume(_store_root):
    """볼륨이 아직 비어 있어도(첫 기동) 조용히 빈 목록 — 기동을 막으면 안 된다."""
    assert ur.clear_locks_at_startup() == []
