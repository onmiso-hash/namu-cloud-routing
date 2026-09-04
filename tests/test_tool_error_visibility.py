"""거절 사유가 AI에게 실제로 닿는지 (namu-tool-error-visibility, 2026-09-04).

SDK 2.x는 도구 호출 중 난 예외를 두 갈래로 나눈다 — `ToolError`는 문구가 그대로
전달되고("예상한 실패"), 그 밖의 예외(이 서버가 던지는 `ValueError` 포함)는
`Error executing tool <이름>` 한 줄만 남기고 원문은 서버 로그에만 남는다.

이 서버는 코어(vendor/namu-agent)를 얹어 쓰지만 **도구를 등록하는 자리는 이
파일에만 있다**. 그래서 개인용 서버를 v0.1.76으로 고쳤어도 이쪽은 따라오지 않았고,
2026-09-04에 실제로 웹에서 거절당한 호출이 사유 없이 한 줄만 돌려주는 것을 확인했다.
`routing_server.tool()`이 그 자리를 감싼다.

감싸는 것은 **도구 호출 경로**뿐이다. 파이썬에서 직접 부르는 경로(모듈 이름
`rs.namu_record` 등, tests/test_routing_server.py의 37곳이 이 경로로 `ValueError`를
잡는다)는 원본 함수 그대로 남긴다.
"""
import asyncio

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

import routing_server as rs


def _call(name: str, args: dict):
    """도구 호출 경로로 부른다 — AI가 부르는 것과 같은 길."""
    return asyncio.run(rs.mcp.call_tool(name, args))


def test_toolcall_delivers_the_valueerror_text():
    """사용자 키가 없어 거절될 때, 그 사유가 문구째로 전달되어야 한다."""
    with pytest.raises(ToolError) as caught:
        _call("namu_recall", {})

    assert "사용자 키" in str(caught.value), str(caught.value)


def test_direct_python_call_still_raises_valueerror():
    """파이썬에서 직접 부르는 경로는 종전대로 ValueError여야 한다.

    이 경로를 함께 바꾸면 `except ValueError`로 잡던 기존 검사 37곳이 조용히
    안 잡히게 된다.
    """
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=None)


def test_toolcall_still_hides_an_unexpected_crash(monkeypatch):
    """진짜 고장은 감춰진 채로 두어야 한다 — 안내문과 결함이 섞이면 안 된다.

    시험용 도구를 진짜 서버에 등록하면 그 뒤 도구 목록을 세는 검사들이
    깨지므로(`test_instructions.py` 포함), 등록 대상을 일회용 서버로 갈아 끼운다.
    `tool()`은 데코레이터가 실행될 때 모듈의 `mcp`를 읽으므로 이 방법이 통한다.
    """
    probe_server = MCPServer("probe")
    monkeypatch.setattr(rs, "mcp", probe_server)

    @rs.tool(name="_probe_crash_tool")
    def _probe_crash_tool() -> str:
        raise KeyError("이건 안내문이 아니라 코드 결함이다")

    with pytest.raises(UnexpectedToolError) as caught:
        asyncio.run(probe_server.call_tool("_probe_crash_tool", {}))

    # UnexpectedToolError는 ToolError의 하위 갈래라 종류만으로는 못 가른다 —
    # 가르는 것은 **원문이 새어 나왔는지**다.
    assert "코드 결함" not in str(caught.value), str(caught.value)


def test_every_exposed_tool_is_still_registered():
    """감싸는 과정에서 도구가 하나라도 빠지지 않았는지 본다."""
    served = asyncio.run(rs.mcp.list_tools())
    names = sorted(t.name for t in served)

    assert names == sorted(rs.EXPOSED_TOOLS), names


def test_wrapped_tool_schema_is_unchanged():
    """껍데기가 도구의 입력 칸 목록을 바꾸지 않아야 한다."""
    served = asyncio.run(rs.mcp.list_tools())
    record = next(t for t in served if t.name == "namu_record")

    props = record.input_schema.get("properties", {})
    for field in ("bowl", "summary", "reason", "body", "topic"):
        assert field in props, f"{field} 칸이 사라졌다: {sorted(props)}"
