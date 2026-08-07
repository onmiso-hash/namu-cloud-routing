"""첨부 파일 주고받기(attach_files) 시험.

실제 GitHub 호출은 여기서 할 수 없으므로 **github_app의 호출 함수 한 겹만** 대역으로
바꾼다 — web_auth 시험이 `_http_json` 한 곳만 가로채는 것과 같은 방식이다. 그 아래
(HTTP 자체)는 대역이 아니라 실물이며, 이 시험이 겨냥하는 것은 그 위층 규칙이다:
어디에 놓이는가 · 무엇을 거절하는가 · 덮어쓸 때 sha를 함께 보내는가.
"""
import base64

import pytest

import attach_files
import github_app


@pytest.fixture(autouse=True)
def _connected(monkeypatch):
    """연결된 회원 한 명 — 저장소 이름과 열쇠를 정해진 값으로 돌려준다."""
    monkeypatch.setattr(
        attach_files, "_repo_and_token", lambda conn, key: ("alice/mem", "tok")
    )


@pytest.fixture()
def calls(monkeypatch):
    """GitHub에 실제로 무엇을 보냈는지 붙잡아 두는 기록장."""
    seen = {"put": [], "delete": [], "raw": [], "list": [], "sha": {}}

    monkeypatch.setattr(
        github_app, "_put_json",
        lambda url, token, body: seen["put"].append((url, body)) or {},
    )
    monkeypatch.setattr(
        github_app, "_delete_json",
        lambda url, token, body: seen["delete"].append((url, body)) or {},
    )
    monkeypatch.setattr(
        github_app, "_get_raw",
        lambda url, token: seen["raw"].append(url) or b"\x00\x01\x02",
    )
    monkeypatch.setattr(
        github_app, "_get_json_list",
        lambda url, token: seen["list"].append(url) or [
            {"type": "file", "path": "attach_file/가.pdf", "size": 10},
            {"type": "dir", "path": "attach_file/하위", "size": 0},
            {"type": "file", "path": "attach_file/나.pdf", "size": 20},
        ],
    )
    monkeypatch.setattr(
        attach_files, "_existing_sha",
        lambda repo, path, token: seen["sha"].get(path),
    )
    return seen


# ---------------------------------------------------------------------------
# 이름 규칙 — 첨부는 attach_file/ 한 칸에만 놓인다
# ---------------------------------------------------------------------------

def test_name_is_placed_under_the_attach_folder():
    assert attach_files.normalize_name("보고서.pdf") == "attach_file/보고서.pdf"
    assert attach_files.normalize_name("attach_file/보고서.pdf") == "attach_file/보고서.pdf"


@pytest.mark.parametrize(
    "bad", ["", "   ", "../밖.txt", "attach_file/../../밖.txt", "하위/폴더.txt", "a\\b.txt"]
)
def test_names_that_escape_the_folder_are_rejected(bad):
    with pytest.raises(attach_files.AttachError):
        attach_files.normalize_name(bad)


# ---------------------------------------------------------------------------
# 올리기
# ---------------------------------------------------------------------------

def test_upload_sends_the_content_to_the_right_path(calls):
    result = attach_files.upload(None, "alice", "설계.pdf", b"\x01\x02", "attach: 올림")

    url, body = calls["put"][0]
    assert url.endswith("/repos/alice/mem/contents/attach_file/%EC%84%A4%EA%B3%84.pdf")
    assert base64.b64decode(body["content"]) == b"\x01\x02"
    assert "sha" not in body  # 새 파일에는 sha를 보내지 않는다
    assert result == {"path": "attach_file/설계.pdf", "bytes": 2, "replaced": False}


def test_upload_over_an_existing_file_sends_its_sha(calls):
    calls["sha"]["attach_file/설계.pdf"] = "abc123"

    result = attach_files.upload(None, "alice", "설계.pdf", b"x", "attach: 올림")

    _url, body = calls["put"][0]
    # sha가 빠지면 GitHub이 덮어쓰기를 거부한다(다른 사람이 그 사이 바꿨는지 가리는 값).
    assert body["sha"] == "abc123"
    assert result["replaced"] is True


def test_upload_rejects_a_file_over_the_limit(calls, monkeypatch):
    monkeypatch.setenv("NAMU_ATTACH_MAX_BYTES", "10")

    with pytest.raises(attach_files.AttachError, match="너무 큽니다"):
        attach_files.upload(None, "alice", "큰것.bin", b"x" * 11, "attach: 올림")

    assert calls["put"] == []  # 거절했으면 GitHub에 아무것도 안 보낸다


def test_upload_rejects_an_empty_file(calls):
    with pytest.raises(attach_files.AttachError, match="빈 파일"):
        attach_files.upload(None, "alice", "빈것.txt", b"", "attach: 올림")


def test_size_limit_is_configurable_and_validated(monkeypatch):
    # 완료조건이 "숫자는 구현 뒤 실제 파일로 재서 채운다"이므로 코드를 고치지 않고
    # 바꿀 수 있어야 한다.
    monkeypatch.setenv("NAMU_ATTACH_MAX_BYTES", "12345")
    assert attach_files.max_bytes() == 12345
    monkeypatch.setenv("NAMU_ATTACH_MAX_BYTES", "숫자아님")
    with pytest.raises(attach_files.AttachError):
        attach_files.max_bytes()
    monkeypatch.setenv("NAMU_ATTACH_MAX_BYTES", "0")
    with pytest.raises(attach_files.AttachError):
        attach_files.max_bytes()


# ---------------------------------------------------------------------------
# 받기 — raw로 요청해야 큰 파일이 조용히 빈 값이 되지 않는다
# ---------------------------------------------------------------------------

def test_download_returns_raw_bytes(calls):
    calls["sha"]["attach_file/그림.bin"] = "sha1"

    assert attach_files.download(None, "alice", "그림.bin") == b"\x00\x01\x02"


def test_download_of_a_missing_file_says_so(calls):
    with pytest.raises(attach_files.AttachError, match="없습니다"):
        attach_files.download(None, "alice", "없는것.pdf")


def test_download_asks_for_the_raw_format():
    """기본 JSON 응답은 1 MiB를 넘으면 오류가 아니라 빈 content를 조용히 준다
    (2026-08-07 실측) — 그 상태를 빈 파일로 읽으면 큰 파일이 0바이트가 된다.
    동작으로는 드러나지 않는 계약이라 소스로 못 박는다."""
    from pathlib import Path

    source = (Path(github_app.__file__)).read_text(encoding="utf-8")
    raw_fn = source[source.index("def _get_raw"):source.index("def _get_json_list")]
    assert "application/vnd.github.raw" in raw_fn


# ---------------------------------------------------------------------------
# 지우기
# ---------------------------------------------------------------------------

def test_delete_sends_the_sha(calls):
    calls["sha"]["attach_file/설계.pdf"] = "abc123"

    path = attach_files.delete(None, "alice", "설계.pdf", "attach: 지움")

    _url, body = calls["delete"][0]
    assert body["sha"] == "abc123"
    assert path == "attach_file/설계.pdf"


def test_delete_of_a_missing_file_says_so(calls):
    with pytest.raises(attach_files.AttachError, match="없습니다"):
        attach_files.delete(None, "alice", "없는것.pdf", "attach: 지움")


# ---------------------------------------------------------------------------
# 목록 — 폴더가 아닌 것은 빼고, 크기는 API가 준 값을 그대로 쓴다
# ---------------------------------------------------------------------------

def test_list_returns_files_with_sizes(calls):
    files = attach_files.list_in_repo(None, "alice")

    assert files == [
        {"path": "attach_file/가.pdf", "bytes": 10},
        {"path": "attach_file/나.pdf", "bytes": 20},
    ]


def test_list_is_empty_when_the_folder_does_not_exist(monkeypatch):
    # GitHub은 폴더가 없으면 404를 준다 — 오류가 아니라 "아직 아무것도 안 올렸다"다.
    monkeypatch.setattr(github_app, "_get_json_list", lambda url, token: [])
    assert attach_files.list_in_repo(None, "alice") == []


def test_commit_message_has_no_line_breaks():
    # 커밋 첫 줄이 곧 제목이라 줄바꿈이 섞이면 이력이 엉뚱하게 잘려 보인다.
    msg = attach_files.commit_message("올림", "attach_file/줄\n바꿈.txt")
    assert "\n" not in msg
