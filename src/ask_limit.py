"""AI 안내원 한도 세기 — 사람별 하루 몇 번, 사이트 전체 하루 몇 번.

설계서: `docs/namu_ai_guide_design.md` 7절.

**무료 등급이라 여기서 막는 것은 돈이 아니다.** Google이 거는 요청 수 제한이
넘을 수 없는 벽이고, 이 파일이 하는 일은 그 벽에 닿기 전에 두 가지를 하는 것이다.

1. **한 사람이 그날 몫을 다 긁는 것을 막는다** — Google의 벽은 누가 썼는지를
   가리지 않으므로, 한 사람이 아침에 다 쓰면 나머지 방문자는 하루 종일 못 쓴다.
2. **딱딱한 오류 대신 부드러운 문구로 받는다** — 우리 총액을 Google 한도보다
   낮게 잡아, 벽에 부딪히기 전에 우리가 먼저 안내한다.

── 왜 파일에 적나 ──

메모리에만 세면 컨테이너가 다시 뜰 때 세던 값이 사라져 한도가 무의미해진다.
`NAMU_STORE_ROOT`는 볼륨이라 다시 떠도 남는다.

── 왜 프로세스 안 잠금 하나로 충분한가 ──

`routing_server.py`의 `uvicorn.run(...)`에 `workers` 인자가 없다(2026-08-08 확인).
한 프로세스이므로 프로세스 사이 잠금이 필요 없다. **나중에 `workers`를 늘리면
이 전제가 깨진다** — 그때는 세기를 파일이 아니라 공용 저장소로 옮겨야 한다.

── 개인정보를 만들지 않는다 ──

접속 주소(IP)와 쿠키 값을 그대로 적지 않고 HMAC 지문으로 바꿔 적는다. 세기에
필요한 것은 "같은 사람인가"뿐이고, 누구인지는 필요 없다. `access_log.fingerprint`
(소금 없는 sha256 앞 8자)를 쓰지 않는 이유: IPv4는 42억 개뿐이라 소금이 없으면
지문을 전부 만들어 보고 원래 주소를 되찾을 수 있다.
"""
import hashlib
import hmac
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_DEFAULT_PER_PERSON = 10
_DEFAULT_DAILY_CAP = 200
"""사이트 전체 하루 한도(설계서 7-1절).

Google 무료 등급 Flash-Lite 계열의 하루 요청 수는 **250~1,000회**다(2026-08-08
회원이 알려준 값). 아직 범위인 이유는 그 값이 Flash 계열 전체를 묶은 것이고,
우리 계정의 한 줄이 정확히 얼마인지는 로그인해야 보이는 화면에만 있기 때문이다.

**낮은 쪽(250)의 80%인 200을 잡는다.** 높게 잡았다가 실제가 낮으면 방문자가
딱딱한 오류를 보고, 낮게 잡으면 우리가 조금 덜 쓰는 것으로 끝난다 — 틀렸을 때의
대가가 한쪽으로 크게 기운다. 정확한 숫자를 확인하면 `NAMU_ASK_DAILY_CAP`로
올린다(다시 빌드하지 않는다).
"""


def _tz() -> ZoneInfo:
    """자정 판정에 쓸 시간대. 코어와 같은 `NAMU_TZ`(기본 Asia/Seoul)를 본다.

    코어의 `cfg.now()`를 부르지 않는 이유가 둘이다. ①`cfg`는 `routing_server`가
    sys.path를 손봐 준 뒤에만 불러올 수 있어, 이 파일만 따로 검사하기 어려워진다.
    ②우리에게 필요한 것은 사람이 읽을 시각이 아니라 **날짜 한 줄**이다.
    """
    return ZoneInfo(os.environ.get("NAMU_TZ", "Asia/Seoul"))


def today(now: "datetime | None" = None) -> str:
    """한국 시각 기준 오늘 날짜. 이 값이 바뀌면 세던 값이 0으로 돌아간다."""
    return (now or datetime.now(_tz())).astimezone(_tz()).strftime("%Y-%m-%d")


def _secret() -> bytes:
    """지문에 섞을 소금. 쿠키 서명과 같은 값을 쓴다.

    `web_auth`에서 가져오지 않고 환경변수를 직접 읽는 이유: `web_auth`가 이
    파일을 부르게 되므로 서로 부르는 관계가 된다. 값이 없으면 지문이 약해질 뿐
    세기 자체는 돌아가야 하므로, 없을 때 멈추지 않고 빈 소금을 쓴다 —
    한도가 개인정보 보호보다 먼저 무너지면 안 된다.
    """
    return os.environ.get("NAMU_SESSION_SECRET", "").encode("utf-8")


def fingerprint(value: str) -> str:
    """되돌릴 수 없는 짧은 지문. 소금을 섞어 전부 만들어 보는 공격을 막는다."""
    return hmac.new(_secret(), value.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:16]


@dataclass(frozen=True)
class Decision:
    """한도 검사 결과."""

    allowed: bool
    reason: str = ""  # "" | "person" | "total"
    message: str = ""  # 방문자에게 그대로 보일 문구
    remaining: int = 0  # 이 사람이 오늘 더 물을 수 있는 횟수


_PERSON_FULL = (
    "오늘은 여기까지입니다. 내일 다시 열립니다 — "
    "그동안은 나무 안내서를 보시면 대부분 답이 있습니다."
)
_SITE_FULL = (
    "오늘 안내원이 받을 수 있는 질문을 다 채웠습니다. 내일 다시 열립니다 — "
    "그동안은 나무 안내서를 보시면 대부분 답이 있습니다."
)


class Limiter:
    """하루 세기 한 벌. 서버가 뜰 때 하나 만들어 계속 쓴다."""

    def __init__(
        self,
        path: "Path | str",
        *,
        per_person: "int | None" = None,
        daily_cap: "int | None" = None,
    ):
        self.path = Path(path)
        self.per_person = per_person if per_person is not None else _env_int(
            "NAMU_ASK_PER_PERSON", _DEFAULT_PER_PERSON
        )
        self.daily_cap = daily_cap if daily_cap is not None else _env_int(
            "NAMU_ASK_DAILY_CAP", _DEFAULT_DAILY_CAP
        )
        self._lock = threading.Lock()

    # -- 파일 읽고 쓰기 ---------------------------------------------------
    def _load(self, day: str) -> dict:
        """오늘치 세기를 읽는다. 날짜가 다르면(자정을 넘겼으면) 0부터 시작한다."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if data.get("date") != day:
            return {"date": day, "total": 0, "people": {}}
        data.setdefault("total", 0)
        data.setdefault("people", {})
        return data

    def _save(self, data: dict) -> None:
        """같은 폴더에 임시로 쓰고 이름을 바꾼다.

        바로 덮어쓰면 쓰는 도중에 컨테이너가 죽었을 때 반쪽짜리 파일이 남고,
        다음 기동에서 읽기가 실패해 세던 값이 통째로 0이 된다.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- 검사 -------------------------------------------------------------
    def peek(self, cookie_id: str, client_ip: str, now: "datetime | None" = None) -> Decision:
        """세지 않고 지금 물어봐도 되는지만 본다(화면을 그릴 때 쓴다)."""
        with self._lock:
            return self._decide(self._load(today(now)), cookie_id, client_ip)

    def check_and_count(
        self, cookie_id: str, client_ip: str, now: "datetime | None" = None
    ) -> Decision:
        """물어봐도 되는지 보고, 되면 한 번 센다.

        **세는 것은 AI를 부르기 전이다.** 부른 뒤에 세면, 부르다 실패했을 때
        세지 않게 되어 실패를 되풀이하는 사람이 한도를 무한히 우회한다.
        """
        with self._lock:
            day = today(now)
            data = self._load(day)
            decision = self._decide(data, cookie_id, client_ip)
            if not decision.allowed:
                return decision
            data["total"] = int(data.get("total", 0)) + 1
            for key in self._keys(cookie_id, client_ip):
                data["people"][key] = int(data["people"].get(key, 0)) + 1
            self._save(data)
            return Decision(allowed=True, remaining=max(decision.remaining - 1, 0))

    def _keys(self, cookie_id: str, client_ip: str) -> "list[str]":
        """이 사람을 가리키는 지문들 — 쿠키와 접속 주소를 따로 센다.

        쿠키는 지울 수 있고 접속 주소는 여럿이 나눠 쓸 수 있다(회사·학교·휴대폰).
        어느 하나도 완전하지 않아서 **둘 다 세고, 둘 중 하나라도 한도를 넘으면
        막는다.** 접속 주소를 바꿔 가며 부르는 것은 이걸로 못 막는다 — 그건
        사이트 전체 한도가 받는다(설계서 7-3절).
        """
        keys = []
        if cookie_id:
            keys.append("c:" + fingerprint(cookie_id))
        if client_ip:
            keys.append("i:" + fingerprint(client_ip))
        return keys or ["c:anon"]

    def _decide(self, data: dict, cookie_id: str, client_ip: str) -> Decision:
        if int(data.get("total", 0)) >= self.daily_cap:
            return Decision(allowed=False, reason="total", message=_SITE_FULL)
        used = max((int(data["people"].get(k, 0)) for k in self._keys(cookie_id, client_ip)), default=0)
        if used >= self.per_person:
            return Decision(allowed=False, reason="person", message=_PERSON_FULL)
        return Decision(allowed=True, remaining=self.per_person - used)


def _env_int(name: str, fallback: int) -> int:
    """환경변수를 정수로. 값이 이상하면 조용히 기본값 — 기동을 멈추지 않는다."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback
