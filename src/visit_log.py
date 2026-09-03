"""방문 신호 남기기 — 화면이 사람 앞에 그려졌을 때만 한 줄 적는다.

접속 기록(traffic_log.py)과 무엇이 다른가:

  접속 기록은 **서버가 받은 두드림**을 센다. 화면 한 장이 열릴 때 브라우저는
  서버를 여러 번 두드리고(자동 갱신·화면 속 호출), 훑기 도구도 똑같이 두드린다.
  그래서 그 숫자로는 사람이 몇 명 왔는지 알 수 없다 — 2026-09-03 실측에서
  포털의 표시값 332건 중 사람이 실제로 연 화면은 101건이었다.

  이 파일은 **화면이 실제로 그려졌을 때** 브라우저가 보내온 신호를 센다.
  자동 갱신은 신호를 보내지 않고, 훑기 도구는 자바스크립트를 돌리지 않으므로
  걸러내는 수고 없이 저절로 빠진다.

  한 줄  = {"t":시각, "svc":서비스, "vid":표, "p":경로, "cc":나라}
  한 파일 = <보관함>/visits/<서비스>-YYYY-MM-DD.jsonl  (하루 한 장)

**하위 폴더에 두는 이유**: 접속자 지도(onnamu-project `portal/traffic_view.py`)는
보관함에 있는 .jsonl을 전부 읽어 서비스별로 센다. 방문 기록을 같은 자리에 두면
그 화면이 이것까지 요청 수에 합산해 버린다. 폴더 하나를 내려 두면 그쪽 눈에는
안 띈다.

지키는 것 셋은 접속 기록과 같다:

1. **통째로 다시 쓰지 않는다.** 여러 줄을 모았다가 한 번에 붙인다.
2. **어떤 실패도 화면으로 새어 나가지 않는다.** 못 적으면 조용히 넘긴다.
3. **모양이 세 곳에서 같아야 한다.** 칸 이름을 바꾸면 셋을 함께 바꾼다.
   **원본은 onnamu-project 저장소**에 있고(`portal/visit_log.py` ·
   `rdap/bootstrap_server/visit_log.py`), 이 파일은 그 중 첫 번째를 그대로
   옮겨온 세 번째 벌이다. 나무 클라우드의 코드가 다른 저장소에 살기 때문에
   import로 나눠 쓸 수가 없어 베껴 두었다. **한쪽만 고치지 말 것** —
   보관함 폴더(`/traffic/visits`)를 세 서비스가 함께 쓰므로, 한 곳이 칸 이름을
   바꾸면 나머지 두 곳의 줄이 그 화면에서 조용히 사라진다.

**이 저장소에서만 다른 것 — 경로에 비밀값이 박혀 있다.** 나무 클라우드의 주소는
`/mcp/<사용자별 열쇠>` · `/u/<티켓 번호>` · `/d/<티켓 번호>` 꼴이라, 브라우저가
보내온 경로를 그대로 적으면 2026-08-01에 접속 기록에서 열쇠를 지운 일(namu-67)이
통째로 무효가 된다. 그래서 **거르는 일은 이 파일이 아니라 받는 자리**
(`web_auth.page_beacon`)가 한다 — 아는 공개 경로가 아니면 경로를 버리고
`"기타"`로 적는다. 이 파일은 넘어온 글자를 그대로 적을 뿐이라 나머지 두 벌과
모양이 같다(규칙 3).
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 접속 기록과 같은 보관함을 쓰되 한 칸 내려간다(위 설명 참고).
TRAFFIC_DIR = os.environ.get("TRAFFIC_DIR", "/traffic")
VISIT_DIR = os.path.join(TRAFFIC_DIR, "visits")
KEEP_DAYS = int(os.environ.get("TRAFFIC_KEEP_DAYS", "30"))

_FLUSH_LINES = 20
_FLUSH_SECONDS = 5.0

# 한 접속 주소가 1분 동안 보낼 수 있는 신호의 상한.
# 사람이 화면을 아무리 빨리 넘겨도 이 수를 넘지 않는다. 넘는 것은 자동화이거나
# 남의 장난이므로 버린다. 접속 주소는 이 판정에만 쓰고 파일에는 적지 않는다.
_RATE_LIMIT = 40
_RATE_WINDOW = 60.0

# 표와 경로의 글자 수 상한. 남이 아무 값이나 보낼 수 있는 자리이므로 잘라 둔다.
_MAX_VID = 64
_MAX_PATH = 200

_lock = threading.Lock()
_buffer = []          # [(파일이름, 한 줄 글자열)]
_last_flush = time.monotonic()
_last_purge = 0.0
_rate = {}            # {접속주소: [세기 시작 시각, 건수]}


def _first_value(get_header, *names):
    for name in names:
        try:
            value = get_header(name)
        except Exception:
            value = None
        if value:
            return str(value).split(",")[0].strip()
    return None


def _allowed(ip):
    """이 접속 주소가 지금 신호를 더 보내도 되는가.

    주소를 모르면 막지 않는다 — 판정할 근거가 없을 때 막으면 멀쩡한 방문이
    사라지고, 그 사실이 화면에 드러나지도 않는다.
    """
    if not ip:
        return True
    now = time.monotonic()
    started, count = _rate.get(ip, (now, 0))
    if now - started >= _RATE_WINDOW:
        started, count = now, 0
    count += 1
    _rate[ip] = (started, count)
    # 낡은 자리를 이따금 치운다 — 안 그러면 주소가 무한정 쌓인다.
    if len(_rate) > 2000:
        for key in [k for k, v in _rate.items() if now - v[0] >= _RATE_WINDOW]:
            _rate.pop(key, None)
    return count <= _RATE_LIMIT


def record(service, vid, path, get_header, direct_ip=None):
    """방문 신호 한 건을 적는다. 실패해도 예외를 밖으로 내보내지 않는다.

    vid 는 브라우저마다 하나씩 심어 둔 표다. 이것이 있어야 같은 사람의
    두 번째 화면을 새 사람으로 세지 않는다. 접속 주소로는 그 구분을 못 한다 —
    한 집에서 셋이 오면 하나로 세고, 휴대전화는 이동하면 주소가 바뀐다.

    접속 주소는 남용을 막는 판정에만 쓰고 파일에는 남기지 않는다. 사람 수를
    세는 데 필요 없는 자료를 굳이 쌓아 둘 이유가 없다.
    """
    try:
        vid = (str(vid or "").strip())[:_MAX_VID]
        if not vid:
            return
        ip = _first_value(get_header, "CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP")
        if not ip:
            ip = (direct_ip or "").strip()
        if not _allowed(ip):
            return
        now = datetime.now(KST)
        line = {
            "t": now.isoformat(timespec="seconds"),
            "svc": service,
            "vid": vid,
            "p": (str(path or "/"))[:_MAX_PATH],
            "cc": _first_value(get_header, "CF-IPCountry") or "",
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
        os.makedirs(VISIT_DIR, exist_ok=True)
        for file_name, texts in grouped.items():
            with open(os.path.join(VISIT_DIR, file_name), "a", encoding="utf-8") as fp:
                fp.write("\n".join(texts) + "\n")
    except Exception:
        # 못 적었으면 버린다. 다시 시도하려고 쌓아두면 메모리가 무한정 는다.
        pass

    _maybe_purge()


def _flush_loop():
    """때가 되면 스스로 쏟아낸다.

    접속 기록(traffic_log.py)은 다음 요청이 들어올 때 대기열을 살핀다. 그쪽은
    훑기까지 섞여 요청이 끊이지 않으므로 그것으로 충분했다. 방문 신호는 화면
    한 장에 한 건뿐이라 훨씬 뜸하게 오고, 그러면 **마지막 한 건이 다음 방문자가
    올 때까지 대기열에 갇힌다.** 2026-09-03 온나무 포털 배포 직후 실제로 두 건을
    보내 한 건만 적히는 것을 확인하고 이 일꾼을 붙였다. 베끼면서 이 일꾼을
    빠뜨리면 같은 결함이 여기서 되풀이된다.

    돌보는 일꾼이므로 서버가 내려갈 때 붙잡지 않는다(daemon).
    """
    while True:
        time.sleep(_FLUSH_SECONDS)
        try:
            flush()
        except Exception:
            pass


def _maybe_purge():
    """보관 기간이 지난 파일을 지운다. 한 시간에 한 번만 살펴본다."""
    global _last_purge
    now = time.monotonic()
    if now - _last_purge < 3600:
        return
    _last_purge = now
    cutoff = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    try:
        for name in os.listdir(VISIT_DIR):
            if not name.endswith(".jsonl"):
                continue
            stamp = name[:-len(".jsonl")][-10:]
            if len(stamp) == 10 and stamp < cutoff:
                os.remove(os.path.join(VISIT_DIR, name))
    except Exception:
        pass


# 이 파일이 다 읽힌 뒤에 일꾼을 띄운다 — 위에서 띄우면 아직 정의되지 않은
# 함수를 부를 여지가 남는다.
threading.Thread(target=_flush_loop, daemon=True, name="visit-flush").start()
