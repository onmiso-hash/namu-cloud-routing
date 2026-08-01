"""namu-67 실측용 아이 프로세스 — 진짜 서버를 띄워 진짜 요청 하나를 받고 끝낸다.

`tests/test_access_log.py`가 이 파일을 별도 프로세스로 돌리고, 그 프로세스가
남긴 접속 기록(표준출력·표준오류)에 개인 열쇠가 있는지 본다.

**왜 같은 프로세스에서 하지 않나.** 두 가지가 프로세스 단위로 한 번뿐이라서다.
(1) FastMCP의 세션 관리자는 인스턴스당 한 번만 기동할 수 있어, 시험 안에서 진짜
서버를 한 번 띄우면 뒤따르는 다른 시험이 같은 이유로 깨진다. (2) uvicorn은
전역 로깅 설정을 통째로 갈아끼운다. 아이 프로세스로 떼면 둘 다 남의 시험에
번지지 않고, 무엇보다 **배포된 컨테이너가 로그를 뱉는 모습 그대로**를 본다.

인자: <포트> <개인 열쇠>
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import access_log  # noqa: E402
import routing_server as rs  # noqa: E402


def main() -> int:
    port = int(sys.argv[1])
    secret = sys.argv[2]

    import uvicorn

    config = uvicorn.Config(
        rs.build_app(),
        host="127.0.0.1",
        port=port,
        log_config=access_log.build_log_config(),
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        print("시험용 서버가 뜨지 않았다", file=sys.stderr)
        return 2

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "namu-67-test", "version": "0"},
            },
        }
    ).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp/{secret}?client=claude",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    status = 0
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                print(f"예상 밖 응답 코드: {response.status}", file=sys.stderr)
                status = 3
    except Exception as exc:  # noqa: BLE001 — 무엇이든 부모에게 보이게 남긴다
        print(f"요청 실패: {exc}", file=sys.stderr)
        status = 4

    time.sleep(0.3)
    server.should_exit = True
    thread.join(timeout=20)
    sys.stdout.flush()
    sys.stderr.flush()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
