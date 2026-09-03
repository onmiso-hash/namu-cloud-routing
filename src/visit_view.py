"""방문 신호를 읽어 사람 수·방문 수·조회 수로 묶는다.

신호를 적는 쪽은 visit_log.py이고, 이 파일은 읽기만 한다.

**세 숫자가 각각 무엇인가**

  방문자 수 — 브라우저마다 심어 둔 표가 몇 종류인가. 같은 사람이 하루에
              열 번 와도 한 명이다.
  방문 횟수 — 한 표의 신호를 시간순으로 늘어놓고, 30분 넘게 끊긴 자리에서
              자른 덩어리의 수. 같은 사람의 아침 방문과 저녁 방문은 둘이다.
  조회 수   — 신호의 건수. 사람이 실제로 넘겨 본 화면의 장수다.

**끊는 기준을 서버에서 정하는 이유**: 브라우저가 방문 번호까지 만들어 보내면
기준을 바꿀 때 이미 쌓인 기록에는 적용할 수 없다. 신호는 날것으로 받아 두고
나누는 일은 읽는 쪽에서 한다 — 그러면 기준을 고쳐도 지난 기록이 함께 따라온다.

**모양이 세 곳에서 같아야 한다.** 적는 쪽(visit_log.py)과 마찬가지로 이 파일도
**원본은 onnamu-project 저장소**에 있다(`portal/visit_view.py` ·
`rdap/bootstrap_server/visit_view.py`). 이 파일은 그 중 첫 번째를 그대로 옮겨온
세 번째 벌이다 — 나무 클라우드의 코드가 다른 저장소에 살아 import로 나눠 쓸
수가 없어 베껴 두었다. **한쪽만 고치지 말 것.**

**`only_service`가 여기서 꼭 필요한 이유**: 보관함 폴더(`/traffic/visits`)는
미니PC의 실제 폴더 하나이고, 포털·도메인 조회·나무 클라우드가 **함께 쓴다**
(docker-compose가 세 컨테이너에 같은 폴더를 물려 준다). 그래서 거르지 않고
읽으면 나무 클라우드 홈페이지가 포털 방문자까지 제 숫자로 세어 보여 준다.
서비스 이름이 하나뿐이라고 이 인자를 지우면 안 된다.
"""

import json
import os
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
TRAFFIC_DIR = os.environ.get("TRAFFIC_DIR", "/traffic")
VISIT_DIR = os.path.join(TRAFFIC_DIR, "visits")

# 몇 분 넘게 끊기면 다음 신호를 새 방문으로 볼지.
SESSION_GAP_MINUTES = 30

# 화면에 실어 보내는 경로 줄 수 상한.
TOP_PATHS = 50

# 저장이 막힌 브라우저(사생활 보호 모드 등)가 그때만 쓰는 임시 표의 머리글자.
# 이런 표는 다음 방문에 다시 만들어지므로 같은 사람을 한 명으로 묶지 못한다.
# 숫자를 정직하게 보이려고 따로 세어 화면에 함께 알린다.
TEMP_PREFIX = "t-"


def _dates_in_range(days):
    today = datetime.now(KST).date()
    return {(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)}


def _files_for(days, only_service=None):
    """기간 안에 드는 기록 파일들. only_service를 주면 그 서비스 것만 고른다.

    보관함 하나를 여러 서비스가 나눠 쓴다. 포털의 접속자 지도는 전부를 한자리에
    모아 보여 주지만, 도메인 조회와 나무 클라우드의 화면은 제 서비스 숫자만
    보여야 한다.
    """
    try:
        names = os.listdir(VISIT_DIR)
    except Exception:
        return []
    wanted = _dates_in_range(days)
    found = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        stem = name[: -len(".jsonl")]
        stamp, service = stem[-10:], stem[:-11]
        if stamp in wanted and service:
            if only_service and service != only_service:
                continue
            found.append((service, os.path.join(VISIT_DIR, name)))
    return sorted(found)


def _parse(stamp):
    try:
        return datetime.fromisoformat(stamp)
    except Exception:
        return None


def _count_sessions(times):
    """한 사람의 신호 시각들을 30분 기준으로 잘라 방문 횟수를 센다."""
    if not times:
        return 0
    times.sort()
    gap = timedelta(minutes=SESSION_GAP_MINUTES)
    sessions = 1
    for before, after in zip(times, times[1:]):
        if after - before > gap:
            sessions += 1
    return sessions


def summarize(days=1, only_service=None):
    per_service = {}       # {서비스: {"조회":n, "표":set()}}
    per_path = {}          # {(서비스, 경로): 조회수}
    per_country = {}       # {나라: {"조회":n, "표":set()}}
    per_bucket = {}        # {시각칸: 조회수}
    times_by_vid = {}      # {표: [시각...]}
    temp_vids = set()
    total_hits = 0
    # 하루·이틀은 시간 단위로, 그보다 길면 날짜 단위로 묶는다.
    by_hour = days <= 2

    for service, path in _files_for(days, only_service):
        try:
            fp = open(path, encoding="utf-8")
        except Exception:
            continue
        with fp:
            for raw in fp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception:
                    continue    # 쓰다 만 줄 하나 때문에 화면 전체가 비면 안 된다

                vid = row.get("vid") or ""
                if not vid:
                    continue
                stamp = row.get("t") or ""
                screen = row.get("p") or "/"
                country = row.get("cc") or "??"

                total_hits += 1
                if vid.startswith(TEMP_PREFIX):
                    temp_vids.add(vid)

                s = per_service.setdefault(service, {"조회": 0, "표": set()})
                s["조회"] += 1
                s["표"].add(vid)

                key = (service, screen)
                per_path[key] = per_path.get(key, 0) + 1

                c = per_country.setdefault(country, {"조회": 0, "표": set()})
                c["조회"] += 1
                c["표"].add(vid)

                bucket = stamp[:13].replace("T", " ") if by_hour else stamp[:10]
                if bucket:
                    per_bucket[bucket] = per_bucket.get(bucket, 0) + 1

                when = _parse(stamp)
                if when:
                    times_by_vid.setdefault(vid, []).append(when)

    방문횟수 = sum(_count_sessions(times) for times in times_by_vid.values())

    서비스별 = sorted(
        ({"서비스": name, "조회": v["조회"], "방문자": len(v["표"])}
         for name, v in per_service.items()),
        key=lambda d: -d["조회"],
    )
    나라별 = sorted(
        ({"나라": code, "조회": v["조회"], "방문자": len(v["표"])}
         for code, v in per_country.items()),
        key=lambda d: -d["조회"],
    )
    화면별 = sorted(
        ({"서비스": svc, "경로": p, "조회": n} for (svc, p), n in per_path.items()),
        key=lambda d: -d["조회"],
    )[:TOP_PATHS]
    시간별 = [{"때": k, "조회": v} for k, v in sorted(per_bucket.items())]

    return {
        "기간일수": days,
        "묶음": "시간" if by_hour else "날짜",
        "끊는기준분": SESSION_GAP_MINUTES,
        "합계": {
            "방문자": len(times_by_vid),
            "방문": 방문횟수,
            "조회": total_hits,
        },
        # 표를 저장하지 못한 브라우저의 수. 이만큼은 같은 사람이 올 때마다
        # 새 사람으로 세어지므로, 방문자 수가 그만큼 부풀어 있을 수 있다.
        "표없는브라우저": len(temp_vids),
        "서비스별": 서비스별,
        "나라별": 나라별,
        "화면별": 화면별,
        "시간별": 시간별,
        "보관함있음": os.path.isdir(VISIT_DIR),
    }
