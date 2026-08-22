"""접속 기록 — 나무 클라우드도 접속 한 건마다 한 줄을 남긴다.

관리자 화면('접속자 지도')은 포털·도메인 조회·스튜디오 세 곳이 같은 폴더에
같은 모양으로 적어 둔 줄을 읽는다. 이 저장소가 네 번째로 합류하는데, 화면 쪽은
고칠 것이 없으므로 **여기서 검사할 것은 "모양이 같은가"와 "비밀값이 안 새는가"**
두 가지다.

특히 두 번째가 이 저장소에만 있는 몫이다 — 이 서버의 주소에는 사용자별 열쇠와
티켓 번호가 박혀 있어서, 경로를 그대로 적으면 2026-08-01에 접속 기록에서 열쇠를
지운 일(namu-67)이 통째로 무효가 된다.
"""
import asyncio
import json
import os
import time

import pytest
from starlette.testclient import TestClient

import routing_server as rs
import traffic_log


@pytest.fixture(autouse=True)
def _traffic_dir(monkeypatch, tmp_path):
    """보관함을 임시 폴더로 돌리고, 모듈에 남아 있는 상태를 비운다.

    traffic_log는 요청마다 파일을 여는 것을 피하려고 줄을 모아 두는 전역 그릇을
    쓴다(미니PC가 느리다). 그 그릇과 시계가 테스트끼리 새면 앞 테스트가 남긴
    줄이 뒤 테스트의 파일에 섞이므로 매번 비운다.
    """
    box = tmp_path / "traffic"
    monkeypatch.setattr(traffic_log, "TRAFFIC_DIR", str(box))
    monkeypatch.setattr(traffic_log, "_buffer", [])
    monkeypatch.setattr(traffic_log, "_last_flush", time.monotonic())
    # 지우기는 한 시간에 한 번만 도는데, 그 시계도 테스트마다 새로 맞춘다.
    monkeypatch.setattr(traffic_log, "_last_purge", time.monotonic())
    return box


def _headers(**kwargs):
    """머리말 이름 → 값. 없는 이름은 None을 돌려준다(웹 틀의 headers.get 흉내)."""
    return kwargs.get


def _lines(box, service="cloud"):
    """보관함에 쌓인 줄 전부를 사전 목록으로."""
    out = []
    for name in sorted(os.listdir(box)):
        if not name.startswith(service + "-"):
            continue
        with open(os.path.join(box, name), encoding="utf-8") as fp:
            out += [json.loads(line) for line in fp if line.strip()]
    return out


# ---------------------------------------------------------------------------
# 한 줄의 모양 — 네 곳이 같아야 한다
# ---------------------------------------------------------------------------
def test_한_줄에_열_칸이_다_있다(_traffic_dir):
    """칸 이름이 하나라도 다르면 관리자 화면이 그 줄을 못 읽는다.

    화면은 파일 이름에서 서비스를, 칸에서 나라·주소·좌표를 읽는다. 네 곳이 같은
    이름을 써야 한다는 것이 traffic_log 머리말의 규칙 3이라, 그 계약을 여기에
    글자 그대로 못 박아 둔다.
    """
    traffic_log.record(
        "cloud", "/faq",
        _headers(**{
            "CF-Connecting-IP": "203.0.113.9", "CF-IPCountry": "KR",
            "CF-IPCity": "Gwangju", "CF-IPLatitude": "35.15472",
            "CF-IPLongitude": "126.91556",
        }),
        status=200,
    )
    traffic_log.flush()

    line, = _lines(_traffic_dir)
    assert set(line) == {"t", "svc", "ip", "via", "cc", "city", "lat", "lon", "path", "st"}
    assert line["svc"] == "cloud"
    assert line["ip"] == "203.0.113.9"
    assert line["via"] == "cf"
    assert line["cc"] == "KR"
    assert line["city"] == "Gwangju"
    assert line["lat"] == "35.15472"
    assert line["lon"] == "126.91556"
    assert line["path"] == "/faq"
    assert line["st"] == 200


def test_응답_코드를_모르면_0으로_적는다(_traffic_dir):
    """화면 규칙(onnamu-project `portal/traffic_view.py`의 `is_valid`)은 `st`가
    없거나 0이면 유효한 것으로 둔다 — 모르는 것을 무효로 처리하면 멀쩡한 접속이
    화면에서 조용히 사라지기 때문이다. 빈 칸이 아니라 숫자 0이어야 한다."""
    traffic_log.record("cloud", "/", _headers(**{"CF-Connecting-IP": "203.0.113.9"}))
    traffic_log.flush()

    line, = _lines(_traffic_dir)
    assert line["st"] == 0


def test_파일_이름이_서비스_하루_한_장이다(_traffic_dir):
    """화면은 목록을 들고 있지 않고 **파일 이름**에서 서비스를 읽는다
    (portal/traffic_view.py의 `stem[:-11]`). 이름이 이 모양이어야 합류한다."""
    traffic_log.record("cloud", "/", _headers(**{"CF-Connecting-IP": "203.0.113.9"}))
    traffic_log.flush()

    name, = os.listdir(_traffic_dir)
    assert name.startswith("cloud-") and name.endswith(".jsonl")
    stamp = name[len("cloud-"):-len(".jsonl")]
    assert len(stamp) == 10 and stamp.count("-") == 2


def test_클라우드플레어를_안_거치면_서버가_본_주소를_쓴다(_traffic_dir):
    """공유기에 열린 포트로 곧장 들어온 접속에는 그것이 유일한 단서다."""
    traffic_log.record("cloud", "/", _headers(), direct_ip="198.51.100.7")
    traffic_log.flush()

    line, = _lines(_traffic_dir)
    assert line["ip"] == "198.51.100.7"
    assert line["via"] == "direct"


def test_꾸밈_파일과_스스로에게_보낸_요청은_안_센다(_traffic_dir):
    """화면 한 장에 딸려오는 글꼴·그림까지 세면 사람 수가 부풀려지고,
    생존 확인(localhost)은 방문자가 아니다."""
    traffic_log.record("cloud", "/asset/wanted-sans-variable.woff2",
                       _headers(**{"CF-Connecting-IP": "203.0.113.9"}))
    traffic_log.record("cloud", "/", _headers(**{"CF-Connecting-IP": "127.0.0.1"}))
    traffic_log.record("cloud", "/", _headers(), direct_ip="::1")
    traffic_log.flush()

    assert not os.path.isdir(_traffic_dir) or _lines(_traffic_dir) == []


# ---------------------------------------------------------------------------
# 통째로 다시 쓰지 않는다 / 실패가 새어 나가지 않는다 (머리말 규칙 1·2)
# ---------------------------------------------------------------------------
def test_줄을_모았다가_한꺼번에_붙인다(_traffic_dir):
    """요청 한 건마다 파일을 열면 CPU가 1개뿐인 미니PC가 같이 멈춘다.
    20줄이 모이거나 5초가 지나야 붙는다."""
    for _ in range(19):
        traffic_log.record("cloud", "/", _headers(**{"CF-Connecting-IP": "203.0.113.9"}))
    assert not os.path.isdir(_traffic_dir), "아직 안 붙었어야 한다"

    traffic_log.record("cloud", "/", _headers(**{"CF-Connecting-IP": "203.0.113.9"}))
    assert len(_lines(_traffic_dir)) == 20


def test_못_적어도_요청_쪽으로_예외가_안_나간다(monkeypatch, tmp_path):
    """기록은 곁다리다 — 기록 때문에 서비스가 멎으면 안 된다.
    보관함 자리에 파일이 놓여 폴더를 못 만드는 상황으로 재현한다."""
    막힌자리 = tmp_path / "막힌자리"
    막힌자리.write_text("폴더가 아니라 파일이다", encoding="utf-8")
    monkeypatch.setattr(traffic_log, "TRAFFIC_DIR", str(막힌자리))

    traffic_log.record("cloud", "/", _headers(**{"CF-Connecting-IP": "203.0.113.9"}))
    traffic_log.flush()  # 예외가 나면 여기서 터진다

    # 다시 시도하려고 쌓아두면 메모리가 무한정 는다 — 버려야 한다.
    assert traffic_log._buffer == []


def test_보관_기간이_지난_파일은_스스로_지운다(monkeypatch, _traffic_dir):
    """30일치만 남긴다(TRAFFIC_KEEP_DAYS). 미니PC의 디스크가 유한하다."""
    os.makedirs(_traffic_dir, exist_ok=True)
    오래된것 = _traffic_dir / "cloud-2000-01-01.jsonl"
    오래된것.write_text("{}\n", encoding="utf-8")
    남길것 = _traffic_dir / "cloud-2999-01-01.jsonl"
    남길것.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(traffic_log, "_last_purge", 0.0)

    traffic_log.record("cloud", "/", _headers(**{"CF-Connecting-IP": "203.0.113.9"}))
    traffic_log.flush()

    assert not 오래된것.exists()
    assert 남길것.exists()


# ---------------------------------------------------------------------------
# 이 저장소에만 있는 몫 — 주소에 박힌 비밀값 가리기
# ---------------------------------------------------------------------------
def test_사용자별_열쇠가_보관함에_안_남는다():
    """`/mcp/<열쇠>`의 열쇠는 그 자체가 비밀번호다(주소를 아는 사람 = 그 사람의
    기억을 읽고 쓸 수 있는 사람). 보관함 파일은 관리자 화면이 읽는 곳이라
    컨테이너 로그보다 오히려 손이 더 많이 닿는다."""
    열쇠 = "aAwdgjg0" + "x" * 35
    적힌것 = rs._traffic_path("/mcp/" + 열쇠)

    assert 열쇠 not in 적힌것
    assert 적힌것.startswith("/mcp/#")
    # 앞 몇 글자를 남기는 방식은 쓰지 않는다 — 원본의 일부가 계속 남는다.
    assert 열쇠[:8] not in 적힌것


def test_티켓_번호가_보관함에_안_남는다():
    """티켓 번호는 32바이트 난수이고, **그 번호를 아는 것이 곧 그 파일을 올리고
    받을 권한**이다(tickets.new_ticket_id). 코어도 로그에는 앞 8자만 적는다."""
    번호 = "AbCdEfGh" + "9" * 35
    for 앞자락 in ("/u/", "/d/"):
        적힌것 = rs._traffic_path(앞자락 + 번호)
        assert 번호 not in 적힌것
        assert 적힌것.startswith(앞자락 + "#")


def test_같은_열쇠는_같은_지문으로_적힌다():
    """한 사람이 얼마나 두드리는지는 여전히 셀 수 있어야 한다 —
    지문이 매번 달라지면 접속자 지도에서 한 사람이 여러 명이 된다."""
    주소 = "/mcp/" + "z" * 43
    assert rs._traffic_path(주소) == rs._traffic_path(주소)
    assert rs._traffic_path(주소) != rs._traffic_path("/mcp/" + "y" * 43)


def test_비밀값이_없는_주소는_글자_하나_안_바뀐다():
    """어디를 두드렸는지가 이 화면의 알맹이다. 멀쩡한 주소까지 가리면
    '무엇을 보러 왔나'를 못 읽는다."""
    for 주소 in ("/", "/faq", "/guide", "/auth/me", "/asset/wanted-sans-variable.woff2"):
        assert rs._traffic_path(주소) == 주소


def test_머리말은_대소문자를_안_가린다():
    """ASGI는 머리말 이름을 소문자 바이트로 주는데, traffic_log는 웹 틀의 관례대로
    `CF-Connecting-IP`처럼 대문자 섞인 이름으로 찾는다. 이 함수가 그 사이를 잇는다
    — 여기가 어긋나면 모든 줄의 나라·좌표가 빈 칸으로 쌓인다."""
    찾기 = rs._header_getter([(b"cf-connecting-ip", b"203.0.113.9"), (b"cf-ipcountry", b"KR")])

    assert 찾기("CF-Connecting-IP") == "203.0.113.9"
    assert 찾기("cf-ipcountry") == "KR"
    assert 찾기("CF-IPCity") is None


# ---------------------------------------------------------------------------
# 요청을 받는 자리에 실제로 붙어 있는가
# ---------------------------------------------------------------------------
def _대역앱(찍은자리):
    async def app(scope, receive, send):
        찍은자리.append(scope.get("type"))
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return app


def test_요청이_지나가면_한_줄이_남는다(_traffic_dir):
    찍은자리 = []
    client = TestClient(rs._TrafficRecorder(_대역앱(찍은자리)))

    r = client.get("/faq", headers={"cf-connecting-ip": "203.0.113.9", "cf-ipcountry": "KR"})
    traffic_log.flush()

    assert r.status_code == 200 and r.text == "ok"  # 요청은 그대로 지나간다
    line, = _lines(_traffic_dir)
    assert (line["ip"], line["cc"], line["path"], line["svc"]) == ("203.0.113.9", "KR", "/faq", "cloud")
    assert line["st"] == 200


def test_열쇠가_틀려_404로_끊긴_요청도_남는다(monkeypatch, tmp_path, _traffic_dir):
    """남의 열쇠를 찍어 보는 두드림이야말로 이 화면으로 봐야 할 것이다.
    기록을 안쪽에 붙였다면 이런 요청은 통째로 안 보인다."""
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))

    client = TestClient(rs.build_app())
    r = client.post("/mcp/" + "z" * 43, headers={"cf-connecting-ip": "203.0.113.9"})
    traffic_log.flush()

    assert r.status_code == 404
    line, = _lines(_traffic_dir)
    assert line["ip"] == "203.0.113.9"
    assert line["path"].startswith("/mcp/#") and "z" * 43 not in line["path"]
    assert line["st"] == 404, (
        "응답 코드가 실려야 화면이 '유효한 요청만 보기'에서 이 두드림을 걸러낸다 — "
        "요청을 받을 때 적으면 이 값을 알 수 없다"
    )


def test_기록이_가장_바깥에_붙어_있다(monkeypatch, tmp_path):
    """'붙인 줄 알았는데 안 붙어 있었다'가 이런 곁다리 배선의 재발 형태다.
    조립 결과를 직접 짚어 둔다 — 안쪽으로 옮기면 여기서 걸린다."""
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")

    app = rs.build_app()

    assert isinstance(app, rs._TrafficRecorder)
    assert isinstance(app.app, rs._AuthOrMcpDispatcher)


def test_기동_신호는_기록하지_않고_그대로_넘긴다(_traffic_dir):
    """기동 신호(lifespan)에는 주소도 머리말도 없다 — 세면 방문자 수가 틀어진다.

    동시에 **막아서도 안 된다.** MCP 앱은 기동 신호를 받아 세션 관리자를 켜므로,
    이 겹이 그것을 삼키면 서버가 통째로 안 뜬다.
    """
    찍은자리 = []
    scope = {"type": "lifespan"}

    async def 아무것도(*_a, **_k):
        return None

    asyncio.run(rs._TrafficRecorder(_대역앱(찍은자리))(scope, 아무것도, 아무것도))
    traffic_log.flush()

    assert 찍은자리 == ["lifespan"], "기동 신호가 안쪽 앱까지 그대로 가야 한다"
    assert not os.path.isdir(_traffic_dir) or _lines(_traffic_dir) == []


def test_기록이_망가져도_요청은_그대로_지나간다(monkeypatch):
    """규칙 2를 배선 쪽에서도 지킨다 — traffic_log가 다 삼키더라도,
    그 앞에서 경로를 짓는 일이 터지면 요청이 같이 죽는다."""
    def 터지는record(*args, **kwargs):
        raise RuntimeError("보관함이 없다")

    monkeypatch.setattr(traffic_log, "record", 터지는record)
    client = TestClient(rs._TrafficRecorder(_대역앱([])))

    assert client.get("/faq").status_code == 200


# ---------------------------------------------------------------------------
# 응답 코드를 적는 시점 (namu-cloud-traffic-log 추가 요청)
# ---------------------------------------------------------------------------
def test_답이_시작되면_바로_적는다(_traffic_dir):
    """몸통이 다 나가기를 기다리지 않는다.

    MCP는 답을 길게 흘려보내는 연결이 있어서, 끝날 때까지 기다리면 그 연결이 살아
    있는 내내 한 줄도 안 남는다. 응답 코드는 시작 알림에 이미 실려 있으므로 더
    기다릴 이유가 없다. 여기서는 **몸통을 보내기 전에** 이미 줄이 적혔는지 본다.
    """
    적힌줄 = []

    async def 흐르는앱(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        traffic_log.flush()
        적힌줄.extend(_lines(_traffic_dir))  # 몸통을 보내기 전 상태
        await send({"type": "http.response.body", "body": b"ok"})

    client = TestClient(rs._TrafficRecorder(흐르는앱))
    client.get("/faq", headers={"cf-connecting-ip": "203.0.113.9"})

    assert len(적힌줄) == 1, "답이 시작됐는데도 아직 안 적혔다"
    assert 적힌줄[0]["st"] == 200


def test_안쪽이_터져도_두드린_사실은_남는다(_traffic_dir):
    """답이 시작되지도 못한 경우다. 그래도 두드린 사실은 남아야 하고, 코드는
    지어내지 않고 0(모름)으로 적는다."""
    async def 터지는앱(scope, receive, send):
        raise RuntimeError("안쪽이 죽었다")

    async def 아무것도(*_a, **_k):
        return None

    scope = {
        "type": "http",
        "path": "/faq",
        "headers": [(b"cf-connecting-ip", b"203.0.113.9")],
        "client": ("172.16.0.1", 1234),
    }
    with pytest.raises(RuntimeError):
        asyncio.run(rs._TrafficRecorder(터지는앱)(scope, 아무것도, 아무것도))
    traffic_log.flush()

    line, = _lines(_traffic_dir)
    assert line["ip"] == "203.0.113.9"
    assert line["st"] == 0


# ---------------------------------------------------------------------------
# 무슨 도구를 불렀나 (`tool` 칸)
#
# 왜 필요했나: 2026-08-23 접속자 지도에 낯선 미국 주소가 떴는데(알고 보니
# claude.ai 웹 채팅), 기록에는 `/mcp/#지문`까지만 있어 **거기서 무엇을 했는지
# 답할 수 없었다.** 컨테이너 로그도 `CallToolRequest`라는 종류만 찍고 이름은 안
# 찍는다. 그래서 도구 **이름 하나**를 줄에 싣는다.
#
# 이 묶음이 지키는 두 가지: (1) 이름이 실제로 찍히는가, (2) **인자는 한 글자도
# 안 새는가.** 도구 인자에는 기억 본문이 통째로 들어 있어서, 새는 순간 이
# 보관함 파일이 기억의 평문 사본이 된다.
# ---------------------------------------------------------------------------
def _본문읽는앱(받은것=None, status=200):
    """안쪽 앱 흉내 — 본문을 끝까지 읽고 답한다(MCP 전송 계층과 같은 순서다)."""
    async def app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body") or b""
            if not message.get("more_body"):
                break
        if 받은것 is not None:
            받은것.append(body)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return app


def _도구부르기(이름, 인자=None):
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": 이름, "arguments": 인자 or {}},
    }).encode()


def _한줄찍기(box, 몸통, 경로="/mcp/" + "z" * 43, 앱=None):
    """가짜 요청 한 건을 기록 겹에 통과시키고 남은 줄 하나를 돌려준다."""
    client = TestClient(rs._TrafficRecorder(앱 or _본문읽는앱()))
    client.post(경로, content=몸통,
                headers={"cf-connecting-ip": "203.0.113.9",
                         "content-type": "application/json"})
    traffic_log.flush()
    line, = _lines(box)
    return line


def test_도구를_부르면_이름이_남는다(_traffic_dir):
    """이 작업의 알맹이다 — 경로만으로는 읽기만 했는지 뭘 써 넣었는지 모른다."""
    line = _한줄찍기(_traffic_dir, _도구부르기("namu_record", {"bowl": "tasks"}))

    assert line["tool"] == "namu_record"
    # 경로는 그대로 가려진 채다 — 이름을 싣는 자리가 가리기를 우회하면 안 된다.
    assert line["path"].startswith("/mcp/#") and "z" * 43 not in line["path"]


def test_인자는_한_글자도_안_남는다(_traffic_dir):
    """도구 인자에는 기억 본문이 통째로 들어온다(summary·reason·body).
    이것이 새면 보관함 파일이 곧 기억의 평문 사본이다."""
    비밀 = {
        "bowl": "learnings",
        "summary": "아무도보면안되는요약",
        "reason": "아무도보면안되는까닭",
        "body": "아무도보면안되는몸통",
        "project": "비밀프로젝트",
    }
    line = _한줄찍기(_traffic_dir, _도구부르기("namu_record", 비밀))

    assert line["tool"] == "namu_record"
    # 줄을 통째로 글자로 만들어 본다 — 어느 칸에 묻어 들어가도 여기서 걸린다.
    적힌글자 = json.dumps(line, ensure_ascii=False)
    for 값 in 비밀.values():
        assert 값 not in 적힌글자, f"인자 값이 기록에 샜다: {값}"
    # 그릇 이름·작업 이름 같은 짧은 것도 이번 범위 밖이다 — 이름 하나만이다.
    assert set(line) == {"t", "svc", "ip", "via", "cc", "city",
                         "lat", "lon", "path", "st", "tool"}


def test_인자에도_name_칸이_있으면_도구_이름만_고른다(_traffic_dir):
    """앞에서부터 `"name"`을 훑어 찾는 방식이었다면 여기서 **인자 값**을 집는다.

    JSON은 칸 순서가 정해져 있지 않아 인자가 먼저 올 수 있고, 인자 쪽에도
    `name`이라는 칸이 얼마든지 있을 수 있다. 자리를 짚어 꺼내야 안전하다.
    """
    몸통 = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "params": {"arguments": {"name": "인자에숨은값"}, "name": "namu_search"},
        "method": "tools/call",
    }).encode()

    line = _한줄찍기(_traffic_dir, 몸통)
    assert line["tool"] == "namu_search"
    assert "인자에숨은값" not in json.dumps(line, ensure_ascii=False)


def test_도구_호출이_아닌_요청에는_칸_자체가_없다(_traffic_dir):
    """빈 글자로라도 칸을 만들면 도구와 무관한 줄까지 나머지 세 곳과 모양이
    달라진다. 칸이 아예 없어야 한다(traffic_log 머리말 규칙 3)."""
    목록보기 = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()

    line = _한줄찍기(_traffic_dir, 목록보기)
    assert "tool" not in line
    assert set(line) == {"t", "svc", "ip", "via", "cc", "city", "lat", "lon", "path", "st"}


def test_이름_모양이_아니면_안_적는다(_traffic_dir):
    """이름 자리에 이름이 아닌 것이 실려 오면(빈 값·아주 긴 글자·줄바꿈 섞인
    글자) 그건 이름이 아니라 남이 넣은 글자다 — 그대로 싣지 않는다."""
    for 수상한이름 in ("", "a" * 200, "이름 아닌 글자", "namu_record\n조작한줄"):
        traffic_log._buffer.clear()
        line = _한줄찍기(_traffic_dir, _도구부르기(수상한이름))
        assert "tool" not in line, f"이름이 아닌 것이 실렸다: {수상한이름!r}"
        for name in os.listdir(_traffic_dir):
            os.remove(os.path.join(_traffic_dir, name))


def test_MCP가_아닌_주소는_본문을_안_들여다본다(_traffic_dir):
    """곁눈질하는 길과 큰 것이 지나는 길을 갈라 놓는다 — 파일을 올리는 티켓
    주소(`/u/…`)는 본문이 통째로 파일이라 한 바이트도 복사하면 안 된다."""
    client = TestClient(rs._TrafficRecorder(_본문읽는앱()))
    client.post("/u/" + "9" * 43, content=b"x" * 5000,
                headers={"cf-connecting-ip": "203.0.113.9"})
    traffic_log.flush()

    line, = _lines(_traffic_dir)
    assert "tool" not in line
    assert line["path"].startswith("/u/#")


# ---------------------------------------------------------------------------
# 곁눈질이 요청을 망가뜨리지 않는가 (큰 요청 · 토막난 요청 · 흘려보내는 답)
# ---------------------------------------------------------------------------
def _토막내_보내기(앱, 토막들, 경로="/mcp/" + "z" * 43):
    """본문을 여러 토막으로 나눠 보내는 진짜 ASGI 호출."""
    scope = {
        "type": "http", "method": "POST", "path": 경로,
        "headers": [(b"cf-connecting-ip", b"203.0.113.9")],
        "client": ("172.16.0.1", 1234),
    }
    남은토막 = list(토막들)
    보낸것 = []

    async def receive():
        if 남은토막:
            토막 = 남은토막.pop(0)
            return {"type": "http.request", "body": 토막, "more_body": bool(남은토막)}
        return {"type": "http.disconnect"}

    async def send(message):
        보낸것.append(message)

    asyncio.run(rs._TrafficRecorder(앱)(scope, receive, send))
    return 보낸것


def test_본문이_토막나서_와도_그대로_전달되고_이름을_찾는다(_traffic_dir):
    """복사만 하고 넘겨주는 것은 안 건드린다 — 안쪽이 받은 본문이 보낸 것과
    한 바이트도 다르면 안 된다."""
    몸통 = _도구부르기("namu_recall", {"긴글": "가" * 3000})
    받은것 = []
    토막들 = [몸통[i:i + 512] for i in range(0, len(몸통), 512)]
    assert len(토막들) > 1

    _토막내_보내기(_본문읽는앱(받은것), 토막들)
    traffic_log.flush()

    assert 받은것 == [몸통], "안쪽 앱이 받은 본문이 달라졌다"
    line, = _lines(_traffic_dir)
    assert line["tool"] == "namu_recall"


def test_본문이_상한을_넘으면_이름은_비우고_요청은_멀쩡하다(monkeypatch, _traffic_dir):
    """상한이 없으면 아무나 100MB를 밀어 넣어 서버 메모리를 밀어낼 수 있다.
    상한을 넘긴 요청은 **이름 고르기만 포기**하고, 요청 자체는 그대로 간다."""
    monkeypatch.setattr(rs, "_MCP_BODY_PEEK_LIMIT", 1000)
    몸통 = _도구부르기("namu_record", {"body": "나" * 5000})
    assert len(몸통) > 1000
    받은것 = []

    보낸것 = _토막내_보내기(_본문읽는앱(받은것), [몸통[:400], 몸통[400:]])
    traffic_log.flush()

    assert 받은것 == [몸통], "상한을 넘겼다고 본문이 잘려서 가면 요청이 죽는다"
    assert 보낸것[0]["status"] == 200
    line, = _lines(_traffic_dir)
    assert "tool" not in line
    assert "나" * 10 not in json.dumps(line, ensure_ascii=False)


def test_흘려보내는_답을_막지_않는다(_traffic_dir):
    """MCP는 답을 여러 토막으로 길게 흘려보낸다(SSE). 본문을 곁눈질한다고
    그쪽이 막히면 도구 호출이 통째로 멎는다."""
    async def 흘리는앱(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        for 조각 in (b"event: message\n", b"data: 1\n", b"\n"):
            await send({"type": "http.response.body", "body": 조각, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    보낸것 = _토막내_보내기(흘리는앱, [_도구부르기("namu_search")])
    traffic_log.flush()

    몸통들 = [m["body"] for m in 보낸것 if m["type"] == "http.response.body"]
    assert 몸통들 == [b"event: message\n", b"data: 1\n", b"\n", b""]
    line, = _lines(_traffic_dir)
    assert line["tool"] == "namu_search"


def test_이름을_고르다_터져도_요청은_그대로_지나간다(monkeypatch, _traffic_dir):
    """규칙 2를 이 겹에서도 지킨다 — 곁다리 때문에 서비스가 멎으면 안 된다."""
    def 터지는고르기(_body):
        raise RuntimeError("고르다 터졌다")

    monkeypatch.setattr(rs, "_tool_name_from_body", 터지는고르기)
    받은것 = []
    보낸것 = _토막내_보내기(_본문읽는앱(받은것), [_도구부르기("namu_record")])
    traffic_log.flush()

    assert 보낸것[0]["status"] == 200
    line, = _lines(_traffic_dir)
    assert "tool" not in line
