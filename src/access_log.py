"""접속 기록(uvicorn access 로그)에서 비밀값을 가린다 (namu-67).

**왜 이 파일이 있나.** namu-59부터 개인 접속 주소는 `/mcp/<사용자별 열쇠>`
형태이고, 그 열쇠 자체가 곧 비밀번호다(주소를 아는 사람 = 그 사람의 기억을
읽고 쓸 수 있는 사람). 그런데 uvicorn의 기본 access 로그는 요청 경로를 그대로
한 줄씩 남기므로, 컨테이너 로그를 볼 수 있는 사람은 전 사용자의 열쇠를 손에
넣는다. 실서버로 재현한 모습:

    INFO: 127.0.0.1:40048 - "POST /mcp/aAwdgjg0…QK_d0?client=claude HTTP/1.1" 200 OK

`_PerUserSecretDispatcher`가 경로를 고정 마운트 경로로 바꿔 넘기는데도 로그에
열쇠가 남는 이유는, 그 미들웨어가 scope를 **복사해서** 고치기 때문이다
(routing_server.py의 `scope = dict(scope)`). uvicorn은 자기가 만든 원본 scope를
보고 로그를 찍으므로 사본의 수정은 로그에 반영되지 않는다. 복사를 그만두는
방식으로 고치면 "로그가 안전한 이유"가 서버 내부 구현의 우연에 기대게 되므로,
여기서는 **기록을 찍는 지점 자체**를 손본다.

**무엇으로 바꾸나.** 열쇠 자리를 되돌릴 수 없는 8자 지문(`#a3f19c2b`)으로
바꾼다. 앞 몇 글자를 남기는 마스킹은 하지 않는다 — 256비트 난수라도 앞부분만
있으면 사실상 식별자가 되고, 무엇보다 열쇠 원본의 일부가 계속 남는다. 지문은
sha256 결과라 원본으로 되돌릴 수 없고, 같은 사람의 요청은 같은 지문으로
찍히므로 "한 사람에게만 생긴 장애인지 전체 장애인지"는 여전히 가릴 수 있다
(2026-08-01 사용자 결정).

곁들여 로그인 왕복의 일회용 자격증명(`?code=`·`?state=`)도 같은 방식으로
가린다 — `/auth/github/callback` 요청 줄에 그대로 남던 값이고, 열쇠와 같은
"접속 기록에 자격증명이 평문으로 남는다"는 한 가지 결함의 다른 얼굴이다.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re

# 지문 앞에 붙는 표식. 이 표식이 있으면 "이미 가려진 값"이라 다시 가리지 않는다
# (같은 줄이 두 번 필터를 지나도 지문의 지문이 되지 않게).
MASK_PREFIX = "#"

_FINGERPRINT_LEN = 8

# `/mcp/<열쇠>` 의 열쇠 조각. 경로 구분자·쿼리 시작 문자·따옴표·공백은 열쇠에
# 들어갈 수 없으므로 거기서 끊는다(access 로그 한 줄은 `"POST /mcp/xxx HTTP/1.1"`
# 처럼 따옴표 안에 들어 있다).
_SECRET_PATH_RE = re.compile(r"(?P<prefix>/mcp/)(?P<secret>[^/?#&\s\"']+)")

# 로그인 왕복의 일회용 자격증명. `?code=`·`&state=` 뒤의 값을 가린다.
_SENSITIVE_QUERY_RE = re.compile(
    r"(?P<prefix>[?&](?:code|state)=)(?P<value>[^&\s\"']+)", re.IGNORECASE
)


def fingerprint(secret: str) -> str:
    """비밀값 → 되돌릴 수 없는 짧은 지문.

    sha256의 앞 8자(16진수 32비트)다. 원본 복원이 불가능하다는 것이 이 함수의
    핵심이고, 길이는 "같은 사람의 요청끼리 묶어 보는" 용도에만 충분하면 된다.
    """
    digest = hashlib.sha256(secret.encode("utf-8", errors="replace")).hexdigest()
    return digest[:_FINGERPRINT_LEN]


def _mask_match(match: "re.Match[str]", group: str) -> str:
    value = match.group(group)
    if value.startswith(MASK_PREFIX):
        # 이미 가려진 값 — 그대로 둔다(두 번 가리면 지문이 또 바뀐다).
        return match.group(0)
    return match.group("prefix") + MASK_PREFIX + fingerprint(value)


def mask(text: str) -> str:
    """로그 한 줄(또는 그 조각)에서 비밀값을 지문으로 바꾼 문자열을 돌려준다.

    비밀값이 없는 줄은 글자 하나 바뀌지 않는다 — 시각·응답 코드·메서드·클라이언트
    주소 같은 장애 진단용 정보는 전부 그대로 남는다(완료조건 2).
    """
    text = _SECRET_PATH_RE.sub(lambda m: _mask_match(m, "secret"), text)
    text = _SENSITIVE_QUERY_RE.sub(lambda m: _mask_match(m, "value"), text)
    return text


def _mask_value(value: object) -> object:
    return mask(value) if isinstance(value, str) else value


class SecretMaskingFilter(logging.Filter):
    """로그 레코드가 손에 닿는 마지막 지점에서 비밀값을 지운다.

    포매팅 결과가 아니라 레코드의 재료(`msg`·`args`)를 고친다 — uvicorn의 access
    로그는 `'%s - "%s %s HTTP/%s" %d'` 꼴이고 경로가 `args`에 들어 있어서, 문자열이
    다 만들어진 뒤가 아니라 여기서 손봐야 어떤 핸들러·포매터를 쓰든 똑같이 가려진다.
    자리(인덱스)를 짚지 않고 문자열인 인자를 전부 훑는 이유도 같다 — uvicorn이
    로그 형식을 바꿔도 따라 깨지지 않는다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_mask_value(a) for a in args)
        elif isinstance(args, dict):
            record.args = {k: _mask_value(v) for k, v in args.items()}
        elif isinstance(args, str):
            record.args = mask(args)
        return True


_FILTER_NAME = "namu_secret_mask"
_FILTER_SPEC = {"()": f"{__name__}.SecretMaskingFilter"}

# 이 이름들이 없으면 필터를 붙일 자리가 사라진 것이다 → 조용히 통과시키지 않고
# 기동을 멈춘다. "붙인 줄 알았는데 안 붙어 있었다"가 이 결함의 재발 형태라서다.
_REQUIRED_LOGGERS = ("uvicorn.access", "uvicorn.error")
_REQUIRED_HANDLERS = ("access", "default")


def build_log_config(base: "dict | None" = None) -> dict:
    """uvicorn에 넘길 로그 설정 — 기본 설정에 비밀값 필터를 얹어 돌려준다.

    로거와 핸들러 **양쪽**에 붙인다. 로거 쪽은 uvicorn이 직접 찍는 줄을 잡고,
    핸들러 쪽은 나중에 다른 로거가 같은 핸들러로 흘러들어와도 잡는다.

    기대한 자리가 없으면 `RuntimeError`를 낸다. uvicorn이 기본 설정 모양을
    바꾸면 필터가 아무 데도 안 붙은 채 서버가 뜨는데, 그건 로그가 다시 열쇠를
    흘리기 시작하는 것과 같으면서도 아무 증상이 없다.
    """
    if base is None:
        import uvicorn.config

        base = uvicorn.config.LOGGING_CONFIG
    config = copy.deepcopy(base)

    filters = config.setdefault("filters", {})
    filters[_FILTER_NAME] = dict(_FILTER_SPEC)

    loggers = config.get("loggers") or {}
    handlers = config.get("handlers") or {}
    missing = [name for name in _REQUIRED_LOGGERS if name not in loggers]
    missing += [name for name in _REQUIRED_HANDLERS if name not in handlers]
    if missing:
        raise RuntimeError(
            "로그 설정에서 비밀값 필터를 붙일 자리를 찾지 못했습니다: "
            f"{', '.join(missing)}. uvicorn의 기본 로그 설정 모양이 바뀌었을 수 "
            "있습니다 — 확인 전까지 서버를 띄우지 않습니다. "
            "Could not find where to attach the secret-masking log filter."
        )

    for section in (loggers, handlers):
        for entry in section.values():
            attached = entry.setdefault("filters", [])
            if _FILTER_NAME not in attached:
                attached.append(_FILTER_NAME)

    return config
