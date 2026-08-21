"""접속 기록 남기기 — 한 줄씩 덧붙이기만 한다.

포털·도메인 조회·크로니클 스튜디오·나무 클라우드 네 곳이 같은 폴더에 같은
모양으로 적고, 관리자 화면이 그 폴더를 읽어 나라·주소·횟수를 센다.

  한 줄  = {"t":시각, "svc":서비스, "ip":접속주소, "cc":나라,
            "city":도시, "lat":위도, "lon":경도, "path":경로,
            "via":"cf" 또는 "direct"}
  한 파일 = <보관함>/<서비스>-YYYY-MM-DD.jsonl  (하루 한 장)

지켜야 할 것 셋:

1. **통째로 다시 쓰지 않는다.** 조회 한 건마다 파일 전체를 다시 쓰면 쓰는
   동안 화면이 같이 멈춘다(2026-08-21 도메인 조회에서 실제로 났던 결함).
   그래서 append 전용이고, 여러 줄을 모았다가 한 번에 붙인다.
2. **어떤 실패도 화면으로 새어 나가지 않는다.** 기록은 곁다리이므로
   못 적으면 조용히 넘긴다 — 기록 때문에 서비스가 멎으면 안 된다.
3. **모양이 네 곳에서 같아야 한다.** 칸 이름을 바꾸면 네 곳을 함께 바꾼다.
   원본은 onnamu-project 저장소에 있고(`portal/traffic_log.py` ·
   `rdap/bootstrap_server/traffic_log.py` · `studio/trafficLog.js`), 이 파일은
   그 중 첫 번째를 그대로 옮겨온 네 번째 벌이다. 나무 클라우드의 코드가 다른
   저장소에 살기 때문에 import로 나눠 쓸 수가 없어 베껴 두었다.

**이 저장소에서만 다른 것.** 나무 클라우드의 주소에는 비밀값이 박혀 있다
(`/mcp/<사용자별 열쇠>` · `/u/<티켓 번호>` · `/d/<티켓 번호>`). 그래서 경로를
그대로 넘기면 안 되고, `routing_server._TrafficRecorder`가 `access_log`를 지나
가린 경로를 넘긴다. 이 파일 자체는 넘어온 글자를 그대로 적을 뿐이다.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 보관함 위치와 보관 기간. 도커가 이 폴더를 세 서비스에 함께 물려 준다.
TRAFFIC_DIR = os.environ.get("TRAFFIC_DIR", "/traffic")
KEEP_DAYS = int(os.environ.get("TRAFFIC_KEEP_DAYS", "30"))

# 몇 줄 모이면 / 몇 초 지나면 파일에 붙일지. 미니PC가 느려서 요청마다 파일을
# 여는 것을 피한다.
_FLUSH_LINES = 20
_FLUSH_SECONDS = 5.0

# 꾸밈 파일은 세지 않는다 — 화면 한 장을 열면 수십 건이 딸려와 사람 수가
# 부풀려지고, 우리가 보려는 것은 "누가 얼마나 두드리나"이기 때문이다.
_ASSET_SUFFIXES = (
    ".css", ".js", ".map", ".ico", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".avif", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".mp3", ".wav",
)

_lock = threading.Lock()
_buffer = []          # [(파일이름, 한 줄 글자열)]
_last_flush = time.monotonic()
_last_purge = 0.0


def _first_value(get_header, *names):
    for name in names:
        try:
            value = get_header(name)
        except Exception:
            value = None
        if value:
            # 'a, b, c' 꼴이면 맨 앞이 방문자다.
            return str(value).split(",")[0].strip()
    return None


def client_ip(get_header):
    """방문자의 진짜 접속 주소.

    서버가 직접 보는 주소는 터널 자신의 주소(172.x)라 쓸 수 없다.
    2026-08-21 실측: Cloudflare가 CF-Connecting-IP 로 붙여 보낸다.
    """
    return _first_value(get_header, "CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP")


def is_asset(path):
    lowered = (path or "").lower()
    if lowered.startswith("/static/") or lowered.startswith("/data/uploads/"):
        return True
    return lowered.endswith(_ASSET_SUFFIXES)


def record(service, path, get_header, direct_ip=None):
    """접속 한 건을 적는다. 실패해도 예외를 밖으로 내보내지 않는다.

    direct_ip 는 서버가 직접 본 주소다. Cloudflare를 거쳐 오면 그것은 터널의
    주소라 쓸모가 없지만, 공유기에 열린 포트로 곧장 들어온 접속에는 그것이
    유일한 단서다. 그래서 머리말이 없을 때만 쓰고 'direct'로 표시해 둔다.
    """
    try:
        if is_asset(path):
            return
        via_cf = True
        ip = client_ip(get_header)
        if not ip:
            via_cf = False
            ip = (direct_ip or "").strip()
        # 우리 서버가 스스로에게 보내는 생존 확인은 방문자가 아니다.
        if not ip or ip in ("127.0.0.1", "::1", "localhost"):
            return
        now = datetime.now(KST)
        # 위도·경도·도시는 Cloudflare에서 '방문자 위치 머리말'을 켜면 붙어 온다.
        # 안 켜져 있으면 빈 칸으로 쌓이고, 켜는 순간부터 저절로 채워진다.
        line = {
            "t": now.isoformat(timespec="seconds"),
            "svc": service,
            "ip": ip,
            "via": "cf" if via_cf else "direct",
            "cc": _first_value(get_header, "CF-IPCountry") or "",
            "city": _first_value(get_header, "CF-IPCity") or "",
            "lat": _first_value(get_header, "CF-IPLatitude") or "",
            "lon": _first_value(get_header, "CF-IPLongitude") or "",
            "path": (path or "")[:200],
        }
        file_name = "%s-%s.jsonl" % (service, now.strftime("%Y-%m-%d"))
        text = json.dumps(line, ensure_ascii=False)
    except Exception:
        return

    with _lock:
        _buffer.append((file_name, text))
        if len(_buffer) >= _FLUSH_LINES or (time.monotonic() - _last_flush) >= _FLUSH_SECONDS:
            _flush_locked()


def flush():
    with _lock:
        _flush_locked()


def _flush_locked():
    global _buffer, _last_flush
    if not _buffer:
        _last_flush = time.monotonic()
        return
    pending, _buffer = _buffer, []
    _last_flush = time.monotonic()

    grouped = {}
    for file_name, text in pending:
        grouped.setdefault(file_name, []).append(text)

    try:
        os.makedirs(TRAFFIC_DIR, exist_ok=True)
        for file_name, texts in grouped.items():
            with open(os.path.join(TRAFFIC_DIR, file_name), "a", encoding="utf-8") as fp:
                fp.write("\n".join(texts) + "\n")
    except Exception:
        # 못 적었으면 버린다. 다시 시도하려고 쌓아두면 메모리가 무한정 는다.
        pass

    _maybe_purge()


def _maybe_purge():
    """보관 기간이 지난 파일을 지운다. 한 시간에 한 번만 살펴본다."""
    global _last_purge
    now = time.monotonic()
    if now - _last_purge < 3600:
        return
    _last_purge = now
    cutoff = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    try:
        for name in os.listdir(TRAFFIC_DIR):
            if not name.endswith(".jsonl"):
                continue
            stamp = name[:-len(".jsonl")][-10:]
            if len(stamp) == 10 and stamp < cutoff:
                os.remove(os.path.join(TRAFFIC_DIR, name))
    except Exception:
        pass
