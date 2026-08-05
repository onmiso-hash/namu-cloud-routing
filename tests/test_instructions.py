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
    assert rs.mcp.instructions == record_input.server_instructions(rs.EXPOSED_TOOLS)


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
