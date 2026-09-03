"""방문 집계 — 화면 한 장이 정확히 한 줄이 되는가.

접속 기록(`traffic_log.py`)이 세는 것은 서버가 받은 두드림이라, 화면 한 장을
열 때 딸려 오는 부속 호출과 훑기 도구가 함께 섞인다. 2026-09-04 온나무 도메인
조회에서 그 숫자를 통계 화면에 그대로 걸었다가 **새로고침 한 번에 제 숫자가
2~3씩 오르는** 것을 실측했고, 고친 방식(방문 신호를 따로 세는 것)을 여기에
그대로 가져왔다.

그래서 이 파일이 지키는 것은 넷이다.

  1. 신호 한 건 = 파일 한 줄. 더도 덜도 아니다.
  2. 숫자를 **읽는 것**으로는 숫자가 변하지 않는다(통계 새로고침).
  3. 보관함을 함께 쓰는 남의 서비스 숫자가 우리 것에 섞이지 않는다.
  4. 경로에 박힌 비밀값(`/mcp/<열쇠>`·`/u/<번호>`)이 보관함에 남지 않는다.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta

import pytest
from starlette.testclient import TestClient

import identity
import routing_server as rs
import ui
import visit_log
import visit_view
import web_auth as wa


# ---------------------------------------------------------------------------
# 공통 픽스처
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _visit_dir(monkeypatch, tmp_path):
    """보관함을 임시 폴더로 돌리고 모듈에 남은 상태를 비운다.

    visit_log는 요청마다 파일을 여는 것을 피하려고 줄을 모아 두는 전역 그릇을
    쓴다. 그 그릇·시계·분당 상한 장부가 테스트끼리 새면 앞 테스트가 남긴 줄이
    뒤 테스트의 파일에 섞이고, 상한에 걸려 멀쩡한 신호가 사라진다.
    """
    box = tmp_path / "traffic" / "visits"
    monkeypatch.setattr(visit_log, "VISIT_DIR", str(box))
    monkeypatch.setattr(visit_log, "_buffer", [])
    monkeypatch.setattr(visit_log, "_last_flush", time.monotonic())
    monkeypatch.setattr(visit_log, "_last_purge", time.monotonic())
    monkeypatch.setattr(visit_log, "_rate", {})
    monkeypatch.setattr(visit_view, "VISIT_DIR", str(box))
    return box


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_SESSION_SECRET", "test-session-secret-value")
    monkeypatch.setenv("NAMU_GITHUB_CLIENT_ID", "Iv1.testclientid")
    monkeypatch.setenv("NAMU_GITHUB_CLIENT_SECRET", "test-client-secret-marker")
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))
    yield


@pytest.fixture
def client():
    return TestClient(wa.build_auth_app(), base_url="https://testserver")


def _headers(**kwargs):
    """머리말 이름 → 값. 없는 이름은 None(웹 틀의 headers.get 흉내)."""
    return kwargs.get


def _lines(box, service="cloud"):
    """보관함에 쌓인 줄 전부를 사전 목록으로."""
    out = []
    if not os.path.isdir(box):
        return out
    for name in sorted(os.listdir(box)):
        if not name.startswith(service + "-"):
            continue
        with open(os.path.join(box, name), encoding="utf-8") as fp:
            out += [json.loads(line) for line in fp if line.strip()]
    return out


def _write_rows(box, service, rows):
    """보관함에 남의 서비스/지난 날짜 줄을 직접 심는다(읽는 쪽 시험용)."""
    os.makedirs(box, exist_ok=True)
    grouped = {}
    for row in rows:
        grouped.setdefault(row["t"][:10], []).append(row)
    for stamp, group in grouped.items():
        with open(os.path.join(box, f"{service}-{stamp}.jsonl"), "a", encoding="utf-8") as fp:
            for row in group:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")


def _now(**delta):
    return (datetime.now(visit_log.KST) + timedelta(**delta)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 1. 신호 한 건 = 줄 하나
# ---------------------------------------------------------------------------
def test_신호_한_건이_줄_하나가_된다(_visit_dir):
    visit_log.record("cloud", "vid-1", "/", _headers())
    visit_log.flush()

    assert len(_lines(_visit_dir)) == 1


def test_같은_사람이_세_번_보내면_세_줄이다(_visit_dir):
    """새로고침 세 번은 조회 세 건이다 — 사람이 정말 세 장을 봤기 때문이다.
    부풀지도(접속 기록의 결함) 줄지도 않아야 한다."""
    for _ in range(3):
        visit_log.record("cloud", "vid-1", "/", _headers())
    visit_log.flush()

    assert len(_lines(_visit_dir)) == 3


def test_한_줄에_다섯_칸이_다_있다(_visit_dir):
    """세 저장소가 같은 보관함에 같은 모양으로 적는다(visit_log.py 규칙 3).
    칸 이름이 어긋나면 읽는 쪽에서 그 줄이 조용히 사라진다."""
    visit_log.record("cloud", "vid-1", "/start", _headers(**{"CF-IPCountry": "KR"}))
    visit_log.flush()

    (row,) = _lines(_visit_dir)
    assert set(row) == {"t", "svc", "vid", "p", "cc"}
    assert row["svc"] == "cloud"
    assert row["vid"] == "vid-1"
    assert row["p"] == "/start"
    assert row["cc"] == "KR"


def test_접속_주소는_파일에_남지_않는다(_visit_dir):
    """사람 수를 세는 데 필요 없는 자료를 쌓아 둘 이유가 없다.
    주소는 분당 상한 판정에만 쓴다."""
    visit_log.record(
        "cloud", "vid-1", "/",
        _headers(**{"CF-Connecting-IP": "203.0.113.7"}),
        direct_ip="192.0.2.9",
    )
    visit_log.flush()

    (row,) = _lines(_visit_dir)
    assert "203.0.113.7" not in json.dumps(row)
    assert "192.0.2.9" not in json.dumps(row)


def test_표가_없는_신호는_아예_적지_않는다(_visit_dir):
    """표가 없으면 같은 사람을 묶을 수가 없어 방문자 수가 뜻을 잃는다."""
    visit_log.record("cloud", "", "/", _headers())
    visit_log.record("cloud", None, "/", _headers())
    visit_log.flush()

    assert _lines(_visit_dir) == []


def test_한_주소가_분당_상한을_넘기면_더_세지_않는다(_visit_dir):
    """사람이 화면을 아무리 빨리 넘겨도 넘지 않는 수다. 넘는 것은 자동화다."""
    for i in range(visit_log._RATE_LIMIT + 15):
        visit_log.record("cloud", f"vid-{i}", "/", _headers(**{"CF-Connecting-IP": "198.51.100.4"}))
    visit_log.flush()

    assert len(_lines(_visit_dir)) == visit_log._RATE_LIMIT


def test_주소를_모르면_막지_않는다(_visit_dir):
    """판정할 근거가 없을 때 막으면 멀쩡한 방문이 조용히 사라진다."""
    for i in range(visit_log._RATE_LIMIT + 5):
        visit_log.record("cloud", f"vid-{i}", "/", _headers())
    visit_log.flush()

    assert len(_lines(_visit_dir)) == visit_log._RATE_LIMIT + 5


def test_계기가_없어도_쏟아내는_일꾼이_돈다():
    """2026-09-03 온나무에서 이 일꾼이 없어 **마지막 한 건이 다음 방문자가 올
    때까지 대기열에 갇혔다.** 베끼면서 빠뜨리기 가장 쉬운 조각이라 여기서 잡는다.
    돌보는 일꾼이므로 서버를 붙잡지 않아야 한다(daemon)."""
    workers = [t for t in threading.enumerate() if t.name == "visit-flush"]

    assert workers, "쏟아내는 일꾼이 없다 — 마지막 신호가 파일에 안 남는다"
    assert all(t.daemon for t in workers)


def test_기록이_실패해도_예외가_밖으로_나가지_않는다(monkeypatch, _visit_dir):
    """기록은 곁다리다. 곁다리 때문에 화면이 멎으면 안 된다."""
    def boom(*_a, **_k):
        raise OSError("보관함을 쓸 수 없다")

    monkeypatch.setattr(visit_log.os, "makedirs", boom)
    visit_log.record("cloud", "vid-1", "/", _headers())
    visit_log.flush()  # 예외가 새어 나오면 여기서 실패한다


# ---------------------------------------------------------------------------
# 2. 읽는 쪽 — 방문자·방문 횟수·조회 수
# ---------------------------------------------------------------------------
def test_같은_표의_여러_신호는_한_사람이다(_visit_dir):
    _write_rows(_visit_dir, "cloud", [
        {"t": _now(), "svc": "cloud", "vid": "a", "p": "/", "cc": "KR"},
        {"t": _now(minutes=-1), "svc": "cloud", "vid": "a", "p": "/faq", "cc": "KR"},
        {"t": _now(minutes=-2), "svc": "cloud", "vid": "b", "p": "/", "cc": "KR"},
    ])

    총계 = visit_view.summarize(1, only_service="cloud")["합계"]

    assert 총계["방문자"] == 2
    assert 총계["조회"] == 3


def test_끊긴_시간이_길면_다음_신호는_새_방문이다(_visit_dir):
    """아침에 왔다 저녁에 다시 온 사람은 한 명이고 방문은 둘이다.
    끊는 기준을 브라우저가 아니라 여기서 정하므로, 기준을 고치면 지난 기록도
    함께 따라온다."""
    gap = visit_view.SESSION_GAP_MINUTES
    _write_rows(_visit_dir, "cloud", [
        {"t": _now(minutes=-(gap * 2)), "svc": "cloud", "vid": "a", "p": "/", "cc": "KR"},
        {"t": _now(minutes=-(gap * 2) + 1), "svc": "cloud", "vid": "a", "p": "/faq", "cc": "KR"},
        {"t": _now(), "svc": "cloud", "vid": "a", "p": "/", "cc": "KR"},
    ])

    총계 = visit_view.summarize(1, only_service="cloud")["합계"]

    assert 총계["방문자"] == 1
    assert 총계["방문"] == 2
    assert 총계["조회"] == 3


def test_남의_서비스_방문은_우리_숫자에_섞이지_않는다(_visit_dir):
    """보관함 폴더는 포털·도메인 조회와 함께 쓰는 미니PC의 실제 폴더 하나다.
    거르지 않으면 홈페이지가 남의 방문자를 제 숫자로 보여 준다."""
    _write_rows(_visit_dir, "cloud", [
        {"t": _now(), "svc": "cloud", "vid": "a", "p": "/", "cc": "KR"},
    ])
    _write_rows(_visit_dir, "portal", [
        {"t": _now(), "svc": "portal", "vid": "p1", "p": "/", "cc": "KR"},
        {"t": _now(), "svc": "portal", "vid": "p2", "p": "/", "cc": "KR"},
    ])

    assert visit_view.summarize(1, only_service="cloud")["합계"]["조회"] == 1
    assert visit_view.summarize(1)["합계"]["조회"] == 3  # 안 거르면 셋이다


def test_표를_저장하지_못한_브라우저는_따로_센다(_visit_dir):
    """이런 표는 올 때마다 새로 만들어져 같은 사람을 묶지 못한다. 방문자 수가
    그만큼 부풀 수 있다는 것을 숨기지 않고 함께 알린다."""
    _write_rows(_visit_dir, "cloud", [
        {"t": _now(), "svc": "cloud", "vid": "t-one", "p": "/", "cc": "KR"},
        {"t": _now(), "svc": "cloud", "vid": "a", "p": "/", "cc": "KR"},
    ])

    assert visit_view.summarize(1, only_service="cloud")["표없는브라우저"] == 1


def test_쓰다_만_줄_하나에_화면_전체가_비지_않는다(_visit_dir):
    os.makedirs(_visit_dir, exist_ok=True)
    stamp = datetime.now(visit_log.KST).strftime("%Y-%m-%d")
    with open(os.path.join(_visit_dir, f"cloud-{stamp}.jsonl"), "w", encoding="utf-8") as fp:
        fp.write('{"t":"' + _now() + '","svc":"cloud","vid":"a","p":"/","cc":"KR"}\n')
        fp.write('{"t":"' + _now() + '","svc":"cloud"\n')  # 쓰다 만 줄

    assert visit_view.summarize(1, only_service="cloud")["합계"]["조회"] == 1


# ---------------------------------------------------------------------------
# 3. 신호를 받는 자리 — POST /api/page
# ---------------------------------------------------------------------------
def test_로그인하지_않아도_신호를_보낼_수_있다(client, _visit_dir):
    """로그인하지 않은 방문자도 세야 하기 때문이다."""
    res = client.post(ui.API_PATHS[0], json={"vid": "vid-1", "p": "/"})
    visit_log.flush()

    assert res.status_code == 204
    assert len(_lines(_visit_dir)) == 1


def test_아는_공개_경로가_아니면_경로를_버린다(client, _visit_dir):
    """이 자리는 누구나 아무 글자나 보낼 수 있고, 보낸 글자는 관리자 화면이
    읽는 보관함 파일에 남는다. 이 서버의 주소에는 사용자 열쇠와 티켓 번호가
    박혀 있으므로(namu-67), 목록에 없는 경로는 통째로 버린다."""
    secret = "CQYRe_Ayo29Cw87KXIrBgmfOua85or0UQQanrPb6tv8"
    client.post(ui.API_PATHS[0], json={"vid": "v1", "p": f"/mcp/{secret}"})
    client.post(ui.API_PATHS[0], json={"vid": "v2", "p": "/u/0123456789abcdef"})
    visit_log.flush()

    쌓인것 = json.dumps(_lines(_visit_dir), ensure_ascii=False)
    assert secret not in 쌓인것
    assert "0123456789abcdef" not in 쌓인것
    # 사람이 온 것은 사실이므로 신호 자체는 버리지 않는다.
    assert len(_lines(_visit_dir)) == 2


def test_공개_경로는_그대로_적힌다(client, _visit_dir):
    client.post(ui.API_PATHS[0], json={"vid": "v1", "p": "/faq"})
    visit_log.flush()

    assert _lines(_visit_dir)[0]["p"] == "/faq"


def test_망가진_본문에도_요청_처리가_깨지지_않는다(client):
    """어떤 실패도 요청 처리로 새어 나가지 않는다."""
    assert client.post(ui.API_PATHS[0], content=b"not json").status_code == 204
    assert client.post(ui.API_PATHS[0], json=["목록은 사전이 아니다"]).status_code == 204
    assert client.post(ui.API_PATHS[0], json={}).status_code == 204


# ---------------------------------------------------------------------------
# 4. 숫자를 읽는 자리 — 읽어도 숫자가 변하지 않아야 한다
# ---------------------------------------------------------------------------
def test_통계를_새로_고쳐도_숫자가_오르지_않는다(client, _visit_dir):
    """이 프로젝트가 이 코드를 만든 이유 그 자체다. 접속 기록을 화면에 걸었을
    때는 화면이 스스로 부르는 호출까지 세어져 새로고침 한 번에 2~3씩 올랐다."""
    client.post(ui.API_PATHS[0], json={"vid": "v1", "p": "/"})
    visit_log.flush()

    처음 = client.get(f"{ui.API_PATHS[1]}?days=1").json()["합계"]
    for _ in range(5):
        client.get(f"{ui.API_PATHS[1]}?days=1")
    visit_log.flush()
    나중 = client.get(f"{ui.API_PATHS[1]}?days=1").json()["합계"]

    assert 처음 == 나중 == {"방문자": 1, "방문": 1, "조회": 1}


def test_통계는_이_서비스_숫자만_돌려준다(client, _visit_dir):
    _write_rows(_visit_dir, "portal", [
        {"t": _now(), "svc": "portal", "vid": "p1", "p": "/", "cc": "KR"},
    ])
    client.post(ui.API_PATHS[0], json={"vid": "v1", "p": "/"})
    visit_log.flush()

    본문 = client.get(f"{ui.API_PATHS[1]}?days=1").json()

    assert 본문["합계"]["조회"] == 1
    assert [줄["서비스"] for 줄 in 본문["서비스별"]] == ["cloud"]


def test_기간은_보관_기간을_넘지_못한다(client):
    """없는 파일을 읽으라고 시키는 것뿐이지만, 상한이 없으면 남이 큰 수를 보내
    폴더를 통째로 훑게 만들 수 있다."""
    assert client.get(f"{ui.API_PATHS[1]}?days=9999").json()["기간일수"] == 30
    assert client.get(f"{ui.API_PATHS[1]}?days=0").json()["기간일수"] == 1
    assert client.get(f"{ui.API_PATHS[1]}?days=하루").json()["기간일수"] == 1


def test_통계는_중간에_끼워지지_않는다(client):
    """끼워 두면 '새로고침해도 안 변한다'는 신고가 들어온다."""
    assert "no-store" in client.get(ui.API_PATHS[1]).headers["cache-control"]


# ---------------------------------------------------------------------------
# 5. 가입한 사람 수
# ---------------------------------------------------------------------------
def test_가입한_사람_수는_장부를_센다(client):
    from contextlib import closing

    with closing(identity.connect()) as conn:
        identity.init_db(conn)
        identity.upsert_user(conn, 1001, "octocat")
        identity.upsert_user(conn, 1002, "hubot")
        identity.upsert_user(conn, 1001, "octocat")  # 같은 사람의 재방문

    assert client.get(ui.API_PATHS[2]).json() == {"회원": 2}


def test_가입한_사람이_누구인지는_나가지_않는다(client):
    """이 숫자는 로그인 없이 누구나 보는 자리로 나간다. 세는 것과 명단은 다르다."""
    from contextlib import closing

    with closing(identity.connect()) as conn:
        identity.init_db(conn)
        identity.upsert_user(conn, 1001, "octocat")

    본문 = client.get(ui.API_PATHS[2]).text

    assert "octocat" not in 본문
    assert "gh-1001" not in 본문


def test_장부를_못_열면_숫자_대신_모른다고_답한다(client, monkeypatch):
    """0을 돌려주면 '아직 아무도 없다'와 '지금 셀 수 없다'가 같은 값이 된다."""
    def boom(*_a, **_k):
        raise RuntimeError("장부를 열 수 없다")

    monkeypatch.setattr(wa.identity, "connect", boom)
    res = client.get(ui.API_PATHS[2])

    assert res.status_code == 503
    assert res.json() == {"회원": None}


# ---------------------------------------------------------------------------
# 6. 문 — 숫자 주소가 인증 쪽으로 새지 않는가
# ---------------------------------------------------------------------------
def test_숫자_주소는_웹_쪽으로_간다():
    """이 셋은 로그인 없이 열려야 한다. 문지기가 보는 목록(`ui.API_PATHS`)과
    실제로 건 자리가 어긋나면 배포한 뒤에야 숫자가 안 뜨는 것으로 드러난다."""
    for path in ui.API_PATHS:
        assert path in rs._WEB_PATHS


def test_숫자_주소를_흉내낸_조작은_닫히는_쪽으로_간다():
    """`==`로만 여는 성질 — 목록에 적힌 그 글자가 아니면 MCP+인증으로 간다."""
    for path in ("/api/page/../mcp", "/api/", "/api/page/x", "/API/page"):
        assert path not in rs._WEB_PATHS


def test_접속_기록과_방문_기록이_같은_이름을_쓴다():
    """두 곳에 따로 적으면 한쪽만 고쳐지는 날 이 서비스가 보관함에 두 이름으로
    쌓인다. 그러면 화면마다 다른 숫자가 뜨는데 어느 쪽도 틀렸다고 말해 주지
    않는다."""
    assert rs._TRAFFIC_SERVICE == wa.SERVICE


# ---------------------------------------------------------------------------
# 7. 신호를 심는 자리 — 공개 화면에만, 한 번만
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", sorted(ui.PUBLIC_PATHS))
def test_공개_화면마다_신호가_한_번씩_붙는다(path):
    import pages
    import pages_en

    out = {**pages.PAGES, **pages_en.PAGES}[path](False)

    assert out.count(ui.API_PATHS[0]) == 1, f"{path}: 신호가 없거나 둘 이상이다"


def test_로그인_뒤_화면과_티켓_화면에는_신호가_붙지_않는다():
    """이 껍데기는 티켓 주소(`/u/<번호>`)에도 쓰인다. 거기까지 세면 신호에
    실어 보낼 경로가 곧 비밀값이 되고, '홈페이지 방문자'에 회원의 작업 화면이
    섞인다."""
    assert ui.API_PATHS[0] not in ui.page("내 페이지", "<p>본문</p>")
    assert ui.API_PATHS[0] not in ui.page("파일 받기", "<p>본문</p>", current="/u/abc")


def test_신호에_실리는_경로는_서버가_박아_넣는다():
    """브라우저가 `location.pathname`을 실어 보내면 이 서버에서는 주소에 박힌
    비밀값이 그대로 따라 나간다. 공개 화면은 경로가 고정이라 서버가 안다."""
    조각 = ui.visit_beacon("/faq")

    assert '"/faq"' in 조각
    assert "location.pathname" not in 조각


def test_신호도_숫자_읽기도_되풀이되지_않는다():
    """되풀이하면 화면을 열어 둔 사람이 시간에 비례해 조회 수를 올린다 —
    '세는 화면이 자기를 밀어 올린다'는 결함의 재발 경로다."""
    import pages

    홈 = pages.PAGES["/"](False)

    assert "setInterval" not in 홈
    assert 홈.count(ui.API_PATHS[1]) == 1
    assert 홈.count(ui.API_PATHS[2]) == 1


def test_홈이_접속_기록이_아니라_방문_신호를_읽는다():
    """rdap이 겪은 결함의 정체가 이것이었다 — 화면이 `traffic_log` 집계를
    읽고 있었고, 방문 신호를 읽는 자리가 아예 없었다."""
    import pages

    홈 = pages.PAGES["/"](False)

    assert ui.API_PATHS[1] in 홈
    assert "/api/stats/traffic" not in 홈


def test_못_읽은_칸은_영이_아니라_모른다로_남는다():
    """0은 '아무도 안 왔다'이고 `—`는 '모른다'다. 둘을 섞으면 화면이 거짓말을
    한다."""
    조각 = ui.live_stats("ko")

    assert 조각.count("—") == len(ui._STAT_FIELDS)
    assert ">0<" not in 조각
