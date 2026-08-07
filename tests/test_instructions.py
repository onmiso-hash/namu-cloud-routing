"""서버 소개문(instructions) 시험.

소개문은 붙는 AI가 연결 직후 **한 번** 받는 자기소개다. 이 서버는 도구 3종만
내주는데, 예전에는 소개문 자체를 안 넘겨(FastMCP에 instructions 인자 없음) 붙은
AI가 네 그릇이 무엇인지 안내받지 못했다. 셀프호스팅 쪽은 반대로 7종을 소개하면서
3종만 내줘, 없는 도구를 부르다 실패했다(2026-08-05 두 서버를 띄워 실측).

지금은 양쪽이 **코어의 같은 함수**(record_input.server_instructions)에 각자
내주는 도구 목록을 넘겨 만든다. 아래 시험은 그 연결이 끊기는 것을 막는다 —
도구를 늘리거나 줄였는데 소개문을 안 고치면 여기서 실패한다.
"""
import asyncio
import re

import record_input
import routing_server as rs


def _named_in_instructions(text: str) -> list[str]:
    return sorted(re.findall(r"- (namu_\w+) —", text))


def test_instructions_exist():
    assert rs.mcp.instructions, "소개문이 비어 있다 — 붙은 AI가 그릇 설명을 못 받는다"


def test_instructions_match_the_tools_actually_served():
    served = sorted(t.name for t in asyncio.run(rs.mcp.list_tools()))
    assert _named_in_instructions(rs.mcp.instructions) == served
    assert served == sorted(rs.EXPOSED_TOOLS)


def test_instructions_come_from_the_core():
    # 손으로 옮겨 적었으면 두 경로가 갈라진다 — 코어 함수 결과와 글자까지 같아야 한다.
    assert rs.mcp.instructions == record_input.server_instructions(
        rs.EXPOSED_TOOLS, upload_takes_path=False,
    )


def _tool_schema(name: str) -> dict:
    tools = {t.name: t for t in asyncio.run(rs.mcp.list_tools())}
    return tools[name].inputSchema


def _tool_line(name: str) -> str:
    prefix = f"- {name} —"
    return next(l for l in rs.mcp.instructions.splitlines() if l.startswith(prefix))


def test_the_upload_line_does_not_offer_a_path_this_server_cannot_take():
    """소개문이 없는 칸을 있다고 말하면 파일을 든 AI가 파일을 읽기 시작한다.

    2026-08-07의 세 번째 base64 사고가 이 자리였다. 소개문에 "디스크에 있는 파일을
    올린다"고 적혀 있었지만 이 서버의 도구는 글자 원문만 받는다. 파일을 작업공간에
    들고 있던 웹 AI는 넣을 칸을 못 찾자 명령을 돌려 파일을 base64로 옮기기
    시작했고(21:04 화면), 회원은 몇 분을 기다리다 응답을 멈췄다.

    도구에서 base64 칸을 없앤 것으로는 이 자리가 안 막힌다 — 인코딩은 우리 도구의
    인자가 아니라 그 앞 단계, AI의 작업공간에서 일어나기 때문이다. 그래서 막는
    자리는 소개문이고, 이 시험은 그 소개가 실제 칸 목록과 어긋나는 것을 잡는다.
    """
    params = set(_tool_schema("namu_upload_file")["properties"])
    assert "file_path" not in params, (
        "이 서버가 파일 경로를 받게 됐다면 소개문의 upload_takes_path도 함께 바꿔야 한다"
    )

    line = _tool_line("namu_upload_file")
    assert "디스크" not in line, f"없는 칸을 있다고 소개하고 있다: {line}"
    assert "namu_create_upload_ticket" in line, (
        "파일로 있는 것이 갈 곳을 안 알려 주면 AI는 파일을 읽는 쪽을 고른다"
    )


def test_instructions_explain_every_bowl():
    text = rs.mcp.instructions
    for bowl in ("learnings", "profile", "tasks", "memo"):
        assert bowl in text


def test_instructions_say_why_the_other_tools_are_missing():
    # 이유를 안 적으면 AI가 없는 이름을 계속 시도한다.
    text = rs.mcp.instructions
    assert "플러그인으로 설치했을 때만" in text
    for missing in ("namu_memo_remove", "namu_task_pin", "namu_task_unpin"):
        assert missing in text


# ---------------------------------------------------------------------------
# 셀프호스팅과의 기능 동등성 (2026-08-05)
#
# 원칙: 셀프호스팅(내 서버에 나무 MCP를 띄운 것)에서 되는 일이 나무 클라우드에서
# 안 되면 안 된다. 실제로 어긋나 있었다 — 코어는 네 종류(교훈·작업일지·개인
# 사실·쪽지)를 다 검색할 수 있는데, 이 서버가 코어를 부르기 **전에** 두 종류만
# 받겠다고 손으로 적어 둔 검사가 있었다. 아래 시험이 그 재발을 막는다.
# ---------------------------------------------------------------------------
import config as cfg


def test_search_accepts_every_bowl_the_core_knows():
    import inspect
    import routing_server as rs_
    src = inspect.getsource(rs_.namu_search)
    # 허용 목록을 손으로 적으면 코어가 늘어날 때 또 갈라진다.
    assert "cfg.BOWL_NAMES" in src, "허용 그릇을 코어에서 가져오지 않고 손으로 적었다"
    assert '("learnings", "tasks")' not in src


def test_core_and_this_server_agree_on_the_bowl_list():
    import db
    assert sorted(cfg.BOWL_NAMES) == sorted(db._VALID_BOWLS)


def test_cloud_and_self_hosted_expose_the_same_tools():
    """개인 주소와 클라우드 주소는 기능이 같아야 한다(2026-08-07 사용자 원칙).

    한쪽에만 도구가 있으면 같은 코어를 쓰는 의미가 없다 — 실제로 첨부 4종이
    클라우드에만 있었고, 사용자가 그것을 짚어 이 시험이 생겼다. 코어의
    `http_server.HTTP_EXPOSED_TOOLS`가 개인 주소가 내주는 목록이므로 그것과
    글자까지 같은지 본다.
    """
    import http_server

    assert rs.EXPOSED_TOOLS == http_server.HTTP_EXPOSED_TOOLS
