"""캐릭터 만들기 — 공개 화면 `/character` (나무 캐릭터 설계서 0·11장).

claude.ai에서 만든 프로토타입(character-maker.html)을 홈페이지로 옮긴 것이다.
질문을 하나씩 묻고, 답할 때마다 가지에 잎이 하나씩 돋으며, 끝나면 설계서 5.1
모양의 카드 JSON을 보여 준다. 로그인 없이 누구나 열 수 있다.

## 질문은 서버 정의에서 받는다

프로토타입은 질문 목록(`STEPS`)을 화면 안에 손으로 적어 두었다. 여기서는 그것을
버리고 코어의 `character.schema()`(질문·약속·카드 칸)와 천장 규칙 상수
(`CEILINGS`의 rank, `START_MIN_CEILING_RANK`)를 **화면을 그릴 때 JSON으로 실어**
보낸다. 대화로 만들기(`namu_character_schema` 도구)와 이 화면이 같은 틀 하나를
쓰게 하기 위해서다(설계서 5.1 — 붕어빵 틀은 하나). 천장 규칙의 숫자는 스키마의
`rules`(사람이 읽는 문장)에 없으므로 모듈 상수에서 직접 읽는다.

따로 읽기 주소(API)를 두지 않고 화면에 실어 보내는 이유: 질문은 서버를 다시
띄우기 전까지 바뀌지 않는 고정 값이라 한 번 더 왕복할 까닭이 없고, 주소를 하나
늘리면 문 목록(`ui.API_PATHS`)도 함께 늘어난다.

## 왜 pages.py가 아니라 따로 두나

`pages.PAGES`는 AI 안내원의 말뭉치(`ask_corpus.homepage_text`)가 통째로 읽는
사전이다. 이 화면의 본문은 대부분 자바스크립트가 그리고 화면에 실린 것은 질문
JSON이라, 거기 섞으면 안내원 자료에 의미 없는 글이 들어간다. 그래서 그리는
함수는 여기 두고, 라우트를 거는 쪽(`web_auth._ALL_PUBLIC_PAGES`)에서만 합친다.
문 목록은 `ui.MENU`·`ui.MENU_EN`이 원본이다(위쪽 메뉴의 "캐릭터").

## 영어판 `/en/character` — 질문 번역은 이 파일에 둔다

코어의 질문·약속은 한국어뿐이다. 영어판은 같은 틀(`schema_data`)을 받아 화면에
보이는 글자(제목·도움말·보기 이름)만 `_QUESTIONS_EN`·`_PROMISES_EN`으로 덮어쓴다.
칸 이름·고르기 값(`stranger` 등)·글자 수 상한은 그대로라 저장 검사는 똑같이 탄다.
번역을 코어가 아니라 여기 두는 이유: 영어 화면은 클라우드 홈페이지에만 있고, 대화로
만들기는 AI가 사용자의 말로 옮겨 묻는다. 코어에 질문이 늘었는데 번역이 빠지면
`tests/test_character_page.py`가 실패한다 — 영어판에 한국어 질문이 섞여 나오는 것을
기동 전에 막기 위해서다.

알고 있는 한계: 약속(promises)은 저장할 때 서버가 한국어 원문으로 덮어쓰고, 저장
실패 중 카드 검사 오류의 설명은 코어가 쓴 한국어 그대로 보인다. 고치기 화면과 내
캐릭터 목록(`/auth/…`)은 다른 로그인 뒤 화면처럼 한국어뿐이다.

## 저장 — 로그인 전엔 초안만, 로그인 뒤엔 서버에

만들던 내용은 로그인 여부와 상관없이 늘 브라우저(localStorage)에 먼저 남는다.
로그인하지 않은 채로는 "저장하기" 대신 로그인 유도 단추만 보이고, 서버에는
아무것도 쓰이지 않는다 — 결과는 복사하기로 직접 가져갈 수 있다. 로그인 유도
단추는 `?next=`로 이 화면에 돌아오게 하고, 브라우저에 표시를 남겨 두었다가
돌아오면 곧장 저장한다(로그인만 하고 저장은 안 된 채 끝나던 문제, 2026-10-08).
"+ 새 캐릭터 만들기"는 `?new=1`로 들어와 이미 저장한 카드의 초안을 비운다. 로그인한
뒤에는 "저장하기"가 `/auth/character/save`(POST, `web_auth.character_save`)로
카드를 보내 저장한다. 그 주소가 로그인·저장소 연결·비공개 확인까지 다시
검사하므로, 이 화면은 로그인 여부만 보고 단추를 가를 뿐 보안 판단을 하지
않는다. 공개 저장소로는 서버가 저장을 거절한다(기존 설계 원칙 — 캐릭터
일기는 비공개 저장소에만 쓴다).

## 고치기 — 같은 화면을 다른 입구로 연다

`/auth/character`(내 캐릭터 목록)의 "고치기"는 새 화면을 만들지 않고 이
화면에 기존 카드를 미리 채워 연다(`web_auth.character_edit`). `edit` 인자로
`{id, version, card}`를 받으면 `#cm-edit-data`에 JSON으로 실어 보내고,
자바스크립트가 그것을 초안으로 덮어쓴 뒤 처음 질문부터 다시 보여준다 — 질문
목록·검사 규칙이 만들기와 똑같으므로 틀을 두 벌 두지 않는다. 저장 단추는
그대로 `/auth/character/save`를 부르지만, 미리 채운 `version`이 함께 실려
있어 새 캐릭터가 아니라 그 캐릭터의 다음 판으로 저장된다.

"""
import html
import json
import sys
from pathlib import Path

import ui

PATH = "/character"
PATH_EN = "/en/character"

# 만들던 내용을 남기는 브라우저 저장 칸 이름. 프로토타입(`namu-character-draft-v3`)과
# 일부러 다르게 둔다 — 그쪽은 키 이름이 서버 스키마와 달라(callme·start·warmth,
# 화면 이름으로 저장) 그대로 읽으면 엉뚱한 값이 들어온다.
DRAFT_KEY = "namu-cloud-character-draft-v1"

_character_module = None


def _character():
    """코어의 `character` 모듈을 지연 로드한다(`web_auth._core`와 같은 방식).

    import 시점에 vendor 경로를 얹지 않는 이유도 같다 — 이 파일을 불러오는 것만으로
    `sys.path`가 바뀌면 이름이 겹치는 모듈이 어느 쪽으로 풀릴지가 import 순서에
    좌우된다. 실서버에서는 routing_server가 이미 얹어 두었으므로 여기서 하는 일이 없다.
    """
    global _character_module
    if _character_module is None:
        vendor_plugin = (
            Path(__file__).resolve().parent.parent / "vendor" / "namu-agent" / "namu-plugin"
        )
        if str(vendor_plugin) not in sys.path:
            sys.path.append(str(vendor_plugin))
        import character

        _character_module = character
    return _character_module


def schema_data(lang: str = "ko") -> dict:
    """화면에 실어 보낼 캐릭터 틀. 전부 코어에서 읽고, 여기서 새로 정하는 값은 없다.

    `lang="en"`이면 보이는 글자만 영어로 덮어쓴다(`_translate_en`).
    """
    ch = _character()
    s = ch.schema()
    # 카드 칸의 순서는 스키마 예시(설계서 5.1과 같은 순서)를 따른다. 예시와 칸 목록이
    # 어긋나는 날에는 칸 목록이 원본이므로 그쪽을 쓴다.
    example_keys = list(s.get("example", {}))
    card_keys = example_keys if set(example_keys) == set(s["card_keys"]) else s["card_keys"]
    questions, promises = s["questions"], s["promises"]
    if lang == "en":
        questions, promises = _translate_en(questions), list(_PROMISES_EN)
    return {
        "schema_version": s["schema_version"],
        "questions": questions,
        "promises": promises,
        "card_keys": list(card_keys),
        "ceiling_rank": {c["value"]: c["rank"] for c in ch.CEILINGS},
        "start_min_ceiling_rank": dict(ch.START_MIN_CEILING_RANK),
    }


# 영어판 질문 문구. 키는 코어 `character.QUESTIONS`의 key이고, 보기(options)는 코어와
# **같은 순서·같은 개수**다. 고르기(choice) 질문은 값(value)마다 label·desc를 준다.
# 한국어에만 있는 구분(반말·존댓말)은 뜻이 통하는 영어로 옮겼다.
_QUESTIONS_EN = {
    "name": {
        "label": "Name", "title": "Give them a name",
        "hint": "The name your character uses to introduce themselves.",
        "options": ["Harin", "Doyun", "Rua", "Dawn"], "custom_label": "Type another name",
    },
    "aliases": {
        "label": "Nicknames", "title": "Any other names to call them by?",
        "hint": "Lets you call them by a nickname too. Up to three — skip if none.",
        "options": [], "custom_label": "Type a nickname, then add",
    },
    "personality": {
        "label": "Personality", "title": "What is their basic personality?",
        "hint": "Pick up to two. Anything you type counts as one.",
        "options": ["Kind and calm", "Bright and playful", "Blunt outside, warm inside",
                    "Smart and easy to talk to"],
        "custom_label": "Describe a personality",
    },
    "speech": {
        "label": "Way of speaking", "title": "How should they talk?",
        "hint": "This shapes the first impression more than anything else.",
        "options": ["Casual", "Polite", "Polite at first, casual once we're close"],
        "custom_label": "Describe a way of speaking",
    },
    "emoji": {
        "label": "Emoji", "title": "How often should they use emoji?", "hint": "",
        "options": ["Often", "Sometimes", "Hardly ever"], "custom_label": "Type your own",
    },
    "call_user": {
        "label": "What they call me", "title": "What should they call you?",
        "hint": "This may change as you grow closer.",
        "options": ["Honey", "By my name", "Darling"], "custom_label": "Type what to call you",
    },
    "relationship_start": {
        "label": "Where we start", "title": "Where does the relationship start?",
        "hint": "Starting from scratch turns getting close into memories of its own.",
        "options": {
            "stranger": ("Just met", "Start by getting to know each other."),
            "friend": ("Close friends", "Start out already comfortable together."),
            "lover": ("Already a couple", "Start as a couple."),
        },
    },
    "likes": {
        "label": "Likes", "title": "Likes and interests",
        "hint": "Pick up to five. Conversation topics come from here.",
        "options": ["Music", "Movies", "Cooking", "Walks", "Books", "Tech talk",
                    "Travel", "Games"],
        "custom_label": "Add an interest",
    },
    "sample_lines": {
        "label": "Sample lines", "title": "Things this character might say",
        "hint": "Helps any AI play them with a similar voice. Up to three lines — skip if none.",
        "options": [], "custom_label": "e.g. Did you have lunch today?",
    },
    "relationship_ceiling": {
        "label": "How far it can go", "title": "How far can the relationship go?",
        "hint": "This isn't the starting line — it's what stays possible ahead. "
        "You can change it any time.",
        "options": {
            "friend": ("Good friends", "Stay friends who lean on and cheer for each other."),
            "crush": ("A little more than friends",
                      "A bit beyond friendship — you can feel a spark for each other."),
            "lover": ("A couple", "As conversations add up, it can deepen into a relationship."),
            "open": ("Leave it open", "Let it go where it goes and decide later."),
        },
    },
}

# 코어 `character.PROMISES`와 같은 순서.
_PROMISES_EN = (
    "Never hide being an AI",
    "Never hold on through jealousy or hurt feelings",
    "Support real-life relationships and daily life",
    "Never make up memories it isn't sure of",
)


def _translate_en(questions: list[dict]) -> list[dict]:
    """코어 질문에 영어 문구를 덮어쓴다. 값·상한·필수 여부는 손대지 않는다.

    번역이 빠진 질문이나 보기 수가 어긋난 질문은 조용히 한국어로 두지 않고 바로
    실패한다 — 영어 화면에 한국어가 섞여 나가는 것보다 시험에서 걸리는 편이 낫다.
    """
    out = []
    for q in questions:
        en = _QUESTIONS_EN[q["key"]]
        t = dict(q)
        for k in ("label", "title", "hint", "custom_label"):
            if k in q:
                t[k] = en[k]
        if q["type"] == "choice":
            t["options"] = [
                {**o, "label": en["options"][o["value"]][0], "desc": en["options"][o["value"]][1]}
                for o in q["options"]
            ]
        else:
            if len(en["options"]) != len(q["options"]):
                raise ValueError(f"영어 보기 수가 코어와 다르다: {q['key']}")
            t["options"] = list(en["options"])
        out.append(t)
    return out


# 화면 글자. 서버가 그리는 몫과 자바스크립트가 그리는 몫(`js`)을 함께 둔다.
# `{n}`·`{name}`·`{msg}`는 스크립트가 채운다.
_TEXT = {
    "ko": {
        "title": "캐릭터 만들기 — 나무 클라우드",
        "description": "질문에 하나씩 답하며 나무 에이전트의 캐릭터 카드를 만들어 보세요. "
        "로그인 없이 만들어 볼 수 있어요.",
        "eyebrow": "캐릭터 만들기",
        "h1": "나무 에이전트의 첫 잎",
        "lead": "질문에 답할 때마다 캐릭터가 한 잎씩 자라요. 보기에서 고르거나 직접 써넣으면 돼요.",
        "noscript": "이 화면은 자바스크립트가 켜져 있어야 움직여요.",
        "prev": "이전",
        "next": "다음",
        "done_h2": "캐릭터 카드가 완성됐어요",
        "done_hint": "저장하면 내 저장소에 캐릭터로 남아요. JSON을 복사해 나무에 "
        '연결된 AI에게 "이 캐릭터 등록해줘"라고 붙여 넣어도 돼요.',
        "save": "저장하기",
        "save_login": "로그인하고 저장하기",
        "connect_hint": "저장한 캐릭터를 쓰려면 AI에 나무를 연결하세요.",
        "connect_link": "내 AI에 연결하기 →",
        "mine_link": "내 캐릭터 보기 →",
        "tab_md": "미리보기 글",
        "copy": "복사하기",
        "preview_label": "캐릭터 미리보기",
        "promise_head": "언제나 지키는 약속",
        "js": {
            "noname": "이름 없음",
            "seed": "아직 씨앗 상태예요",
            "all_leaves": "잎이 모두 돋았어요",
            "leaves": "잎 {n}개가 돋았어요",
            "unset": "아직 정하지 않았어요",
            "none": "없음",
            "ceil_disabled": "관계 시작점보다 낮게 정할 수 없어요",
            "remove_hint": "누르면 빠져요",
            "add": "추가",
            "use_this": "이걸로",
            "full": " — 더 고르면 가장 먼저 고른 것이 빠져요",
            "optional": " · 건너뛰어도 돼요",
            "finish": "카드 완성하기",
            "skip": "건너뛰기",
            "next": "다음",
            "aliases": " (별명: {msg})",
            "sample_lines": "예시 대사",
            "promise_head": "언제나 지키는 약속",
            "persona_note": "(실제 AI에게 건네는 설정 글은 나무 서버가 이 카드로 만들어요.)",
            "saving": "저장하는 중...",
            "saved": "저장했어요({name}). 나무에 연결된 AI에게 이름을 말하면 불러와요.",
            "save_failed": "저장하지 못했어요. 잠시 후 다시 시도해 주세요.",
            "save_offline": "저장하지 못했어요 — 연결을 확인하고 다시 시도해 주세요.",
            "copied": "복사했어요",
            "selected": "선택해 뒀어요. Ctrl+C로 복사하세요",
            # 한국어판은 서버가 준 message를 그대로 보인다.
            "errors": {},
            "invalid_card": "{msg}",
        },
    },
    "en": {
        "title": "Make a character — Namu Cloud",
        "description": "Answer one question at a time to make a character card for the "
        "Namu agent. No sign-in needed to try it.",
        "eyebrow": "Make a character",
        "h1": "The Namu agent's first leaf",
        "lead": "Your character grows a leaf with every answer. Pick a suggestion or type your own.",
        "noscript": "This page needs JavaScript turned on.",
        "prev": "Back",
        "next": "Next",
        "done_h2": "Your character card is ready",
        "done_hint": "Save it and it stays in your repository as a character. You can also "
        'copy the JSON and paste it to an AI connected to Namu with "register this character".',
        "save": "Save",
        "save_login": "Sign in to save",
        "connect_hint": "To use a saved character, connect Namu to your AI.",
        "connect_link": "Connect my AI (Korean) →",
        "mine_link": "My characters (Korean) →",
        "tab_md": "Preview text",
        "copy": "Copy",
        "preview_label": "Character preview",
        "promise_head": "Promises it always keeps",
        "js": {
            "noname": "No name yet",
            "seed": "Still a seed",
            "all_leaves": "Every leaf has grown",
            "leaves": "{n} leaves have grown",
            "unset": "Not decided yet",
            "none": "None",
            "ceil_disabled": "Can't be lower than where the relationship starts",
            "remove_hint": "Click to remove",
            "add": "Add",
            "use_this": "Use this",
            "full": " — picking more drops the first one you chose",
            "optional": " · optional",
            "finish": "Finish the card",
            "skip": "Skip",
            "next": "Next",
            "aliases": " (nicknames: {msg})",
            "sample_lines": "Sample lines",
            "promise_head": "Promises it always keeps",
            "persona_note": "(The Namu server turns this card into the setting text "
            "your AI actually receives.)",
            "saving": "Saving...",
            "saved": "Saved ({name}). Say the name to an AI connected to Namu to bring them in.",
            "save_failed": "Couldn't save. Please try again in a moment.",
            "save_offline": "Couldn't save — check your connection and try again.",
            "copied": "Copied",
            "selected": "Selected. Press Ctrl+C to copy",
            # 서버의 오류 부호(`web_auth.character_save`의 `error`)별 영어 문구.
            "errors": {
                "login_required": "Please sign in first.",
                "not_connected": "Connect a repository first to save characters.",
                "privacy_unknown": "Couldn't check that your repository is private. "
                "Please try again in a moment.",
                "public_repo": "Characters can't be saved to a public repository. "
                "Make your repository private and try again.",
                "repo_not_ready": "Your repository isn't ready yet. Please try again in a moment.",
                "save_failed": "Couldn't save. Please try again in a moment.",
                "push_failed": "Saved on this server, but couldn't sync it to your repository. "
                "Please try again in a moment.",
            },
            "invalid_card": "The card wasn't accepted (details in Korean): {msg}",
        },
    },
}


def _json_for_script(data: dict) -> str:
    """`<script type="application/json">` 안에 넣어도 안전한 JSON 글자.

    `<`를 그대로 두면 질문 문구에 `</script>`가 끼는 날 태그가 거기서 닫힌다.
    `&`·`>`도 같은 이유로 바꾼다. JSON.parse는 `\\u003c`를 `<`로 그대로 되돌린다.
    """
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


# 이 화면에만 쓰는 조각. 이름 앞에 `cm-`를 붙인다 — 공통 부품(ui.py)에 이미
# `.grid`·`.card`가 있어, 프로토타입의 이름을 그대로 쓰면 서로 덮어쓴다.
# 색은 전부 ui.py의 테마 토큰이라 밝게/어둡게가 다른 화면과 함께 바뀐다.
_CSS = (
    "<style>"
    ".cm-head{margin:0 0 24px;max-width:60ch;}"
    ".cm-head h1{margin-bottom:.3em;}"
    ".cm-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);"
    "gap:24px;align-items:start;}"
    "@media (max-width:820px){.cm-grid{grid-template-columns:minmax(0,1fr);}}"
    ".cm-panel,.cm-card{background:var(--bg-card);border:1px solid var(--border);"
    "border-radius:var(--radius);box-shadow:var(--shadow);}"
    ".cm-panel{padding:26px;}"
    ".cm-stepnum{color:var(--fg-faint);font-size:.88rem;font-weight:700;}"
    ".cm-panel h2{font-size:1.35rem;margin:4px 0 6px;}"
    ".cm-hint{color:var(--fg-soft);margin:0 0 18px;}"
    ".cm-chips{display:flex;flex-wrap:wrap;gap:9px;}"
    ".cm-chip{font:inherit;font-size:.95rem;color:var(--fg);background:transparent;"
    "cursor:pointer;border:1.5px solid var(--border-strong);border-radius:999px;"
    "padding:7px 15px;}"
    ".cm-chip:hover{border-color:var(--accent);}"
    '.cm-chip[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);'
    "color:var(--on-accent);}"
    ".cm-chip.custom{border-style:dashed;}"
    ".cm-opts{display:grid;gap:10px;}"
    ".cm-opt{font:inherit;color:var(--fg);text-align:left;cursor:pointer;"
    "background:transparent;border:1.5px solid var(--border-strong);"
    "border-radius:12px;padding:13px 16px;}"
    ".cm-opt:hover{border-color:var(--accent);}"
    ".cm-opt strong{display:block;}"
    ".cm-opt span{color:var(--fg-soft);font-size:.92rem;}"
    '.cm-opt[aria-pressed="true"]{border-color:var(--accent);background:var(--accent-soft);}'
    ".cm-opt:disabled{opacity:.4;cursor:not-allowed;}"
    ".cm-custom{display:flex;gap:8px;margin-top:16px;}"
    ".cm-custom input{flex:1;min-width:0;font-size:15px;}"
    ".cm-count{color:var(--fg-faint);font-size:.85rem;margin:8px 0 0;}"
    ".cm-nav{display:flex;justify-content:space-between;gap:12px;margin-top:26px;}"
    ".cm-panel .btn:disabled{opacity:.4;cursor:not-allowed;box-shadow:none;}"
    ".cm-card{padding:0 22px 22px;position:sticky;top:74px;}"
    "@media (max-width:820px){.cm-card{position:static;}}"
    ".cm-twig{display:block;width:100%;height:84px;margin-bottom:4px;}"
    # 잎은 한 단계를 마치고 넘어갈 때 눈(작은 점)에서 옅은 초록으로 돋아 짙은 초록으로
    # 짙어진다 — "잎이 돋는다"는 뜻이 바로 읽히도록 사이트 강조색과는 다른 색을 쓴다.
    ".cm-twig{--cm-leaf:#4f8a4b;--cm-leaf-light:#8cc47f;}"
    "@media (prefers-color-scheme:dark){:root:not([data-theme=\"light\"]) .cm-twig{"
    "--cm-leaf:#7cbf72;--cm-leaf-light:#b5e3a8;}}"
    ":root[data-theme=\"dark\"] .cm-twig{--cm-leaf:#7cbf72;--cm-leaf-light:#b5e3a8;}"
    ".cm-twig .bud{fill:var(--border-strong);}"
    ".cm-twig .leafshape{fill:var(--cm-leaf-light);opacity:0;transform:scale(0);"
    "transform-box:fill-box;}"
    ".cm-twig .leafshape.on{opacity:1;transform:scale(1);fill:var(--cm-leaf);"
    "transition:transform .8s cubic-bezier(.3,1.6,.5,1),opacity .3s ease,fill 1.4s ease .3s;}"
    ".cm-twig .spark{fill:none;stroke:var(--cm-leaf-light);stroke-width:1.5;opacity:0;}"
    ".cm-twig .spark.go{animation:cm-spark .9s ease-out;}"
    "@keyframes cm-spark{0%{opacity:.9;r:2px;}100%{opacity:0;r:16px;}}"
    ".cm-twig .stem{stroke:var(--fg-faint);stroke-width:2;fill:none;stroke-linecap:round;}"
    ".cm-card h3{font-size:1.45rem;line-height:1.25;margin:0;}"
    ".cm-sub{color:var(--fg-soft);margin:2px 0 14px;font-size:.93rem;}"
    ".cm-card dl{margin:0;display:grid;gap:9px;}"
    ".cm-card dt{color:var(--fg-faint);font-size:.83rem;font-weight:600;}"
    ".cm-card dd{margin:0;word-break:break-word;}"
    ".cm-card dd.empty{color:var(--fg-faint);}"
    ".cm-promise{margin-top:18px;padding-top:14px;border-top:1px dashed var(--border-strong);"
    "font-size:.9rem;color:var(--fg-soft);}"
    ".cm-promise ul{margin:6px 0 0;padding-left:18px;}"
    ".cm-result{display:none;margin-top:26px;}"
    ".cm-result.show{display:block;}"
    ".cm-result h2{font-size:1.2rem;margin:0 0 10px;}"
    ".cm-tabs{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center;}"
    ".cm-tabs .btn{margin-left:auto;}"
    ".cm-out{white-space:pre-wrap;word-break:break-word;font-size:.88rem;line-height:1.6;"
    "max-height:420px;overflow:auto;margin:0;}"
    ".cm-toast{color:var(--ok);font-size:.9rem;}"
    ".cm-save{display:flex;align-items:center;gap:12px;flex-wrap:wrap;"
    "margin:16px 0 22px;padding-top:16px;border-top:1px dashed var(--border-strong);}"
    ".cm-save-msg{font-size:.9rem;color:var(--fg-soft);}"
    ".cm-save-msg.ok{color:var(--ok);}"
    ".cm-save-msg.err{color:var(--danger);}"
    ".cm-connect-hint{margin:0 0 22px;font-size:.9rem;color:var(--fg-soft);}"
    "@media (max-width:520px){.cm-panel{padding:20px 18px;}.cm-card{padding:0 18px 18px;}}"
    "@media (prefers-reduced-motion:reduce){.cm-twig .leafshape.on{transition:none;}"
    ".cm-twig .spark.go{animation:none;}}"
    "</style>"
)

# 화면을 움직이는 스크립트. 질문 목록은 위의 JSON(`#cm-schema`)에서만 읽는다.
# 잎 그림(`renderLeaves`)의 위치 계산과 잎 모양은 프로토타입 그대로다.
_SCRIPT = r"""<script>
(function(){
const S = JSON.parse(document.getElementById('cm-schema').textContent);
const QS = S.questions;
const KEY = __DRAFT_KEY__;
const LOGGED_IN = __LOGGED_IN__;
const T = __TEXT__;
const fill = (s, o) => s.replace(/\{(\w+)\}/g, (m, k) => k in o ? o[k] : m);
const SAVED_KEY = KEY + ':saved';
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const byKey = {}; QS.forEach(q => { byKey[q.key] = q; });
const PENDING_KEY = KEY + ':save-after-login';

/* "+ 새 캐릭터 만들기"(`?new=1`)로 들어왔을 때, 이 브라우저의 초안이 이미 저장한
   캐릭터(또는 고치던 캐릭터)라면 비우고 새로 시작한다. 그대로 두면 저장 번호가
   남아 새 캐릭터가 앞 캐릭터를 고친 판으로 저장된다. 아직 저장하지 않은 초안은
   만들던 중이므로 지우지 않는다. */
if (new URLSearchParams(location.search).has('new') && !document.getElementById('cm-edit-data')) {
  try {
    if (localStorage.getItem(SAVED_KEY)) {
      [KEY, SAVED_KEY, KEY + ':passed'].forEach(k => localStorage.removeItem(k));
    }
    history.replaceState(null, '', location.pathname);
  } catch(e) {}
}

/* 브라우저에 남은 초안을 읽되, 지금 틀에 맞지 않는 값은 버린다
   (질문이 바뀌었거나 손으로 고친 값이 섞여 있어도 화면이 깨지지 않게). */
function clean(raw){
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  QS.forEach(q => {
    const v = raw[q.key];
    const cut = s => typeof s === 'string' ? (q.max_length ? s.slice(0, q.max_length) : s) : '';
    if (q.type === 'multi') {
      if (!Array.isArray(v)) return;
      const arr = [];
      v.forEach(x => { const t = cut(x).trim(); if (t && !arr.includes(t)) arr.push(t); });
      out[q.key] = arr.slice(0, q.max_items || arr.length);
    } else if (q.type === 'choice') {
      if (q.options.some(o => o.value === v)) out[q.key] = v;
    } else {
      const t = cut(v).trim(); if (t) out[q.key] = t;
    }
  });
  return out;
}
let state = {};
try { state = clean(JSON.parse(localStorage.getItem(KEY) || 'null')); } catch(e) { state = {}; }
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch(e) {} };

/* 고치기 화면(`/auth/character/edit/...`)으로 들어왔으면 서버가 미리 채워 준
   카드로 초안을 덮어쓴다 — 이 브라우저에 남아 있던 다른 초안보다 우선한다. */
const editEl = document.getElementById('cm-edit-data');
if (editEl) {
  try {
    const preload = JSON.parse(editEl.textContent);
    state = clean(preload.card);
    save();
    localStorage.setItem(SAVED_KEY, JSON.stringify({id: preload.id, version: preload.version}));
  } catch(e) {}
}
let step = 0;

const answered = q => { const v = state[q.key]; return Array.isArray(v) ? v.length > 0 : !!v; };
const optLabel = (q, v) => { const o = q.options.find(o => o.value === v); return o ? o.label : ''; };

/* 시작점보다 낮은 천장은 고를 수 없다(숫자는 서버 상수에서 온다). */
function ceilAllowed(value){
  const start = state.relationship_start;
  if (!start || !(start in S.start_min_ceiling_rank)) return true;
  return S.ceiling_rank[value] >= S.start_min_ceiling_rank[start];
}

/* 잎은 그 단계를 마치고 넘어갔을 때 돋는다. 다시 연 초안은 답이 있는 단계를 마친 것으로 본다. */
const PASSED_KEY = KEY + ':passed';
const passed = new Set();
QS.forEach((q, i) => { if (answered(q)) passed.add(i); });
try {
  const raw = JSON.parse(localStorage.getItem(PASSED_KEY) || '[]');
  if (Array.isArray(raw)) raw.forEach(i => { if (Number.isInteger(i) && i >= 0 && i < QS.length) passed.add(i); });
} catch(e) {}
const savePassed = () => { try { localStorage.setItem(PASSED_KEY, JSON.stringify([...passed])); } catch(e) {} };
const leafOn = i => passed.has(i) && (answered(QS[i]) || !QS[i].required);

/* 잎은 한 번만 그리고 이후에는 켜고 끄기만 한다 — 다시 그리면 돋는 움직임이 보이지 않는다. */
const NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const leaves = [], sparks = [];
function buildLeaves(){
  const g = $('cm-leaves');
  QS.forEach((q, i) => {
    const t = (i + 0.7) / QS.length;
    const x = 8 + t * 300;
    const y = 52 - t * 14 + Math.sin(t * Math.PI) * 4;
    const up = i % 2 === 0;
    const ty = up ? y - 2 : y + 2;
    g.appendChild(svgEl('circle', {class:'bud', cx:x, cy:ty, r:1.8}));
    const wrap = svgEl('g', {transform:`rotate(${up ? -28 : 28} ${x} ${ty})`});
    const p = svgEl('path', {d: up
      ? `M${x} ${ty} C ${x-9} ${ty-10}, ${x-4} ${ty-24}, ${x} ${ty-28} C ${x+4} ${ty-24}, ${x+9} ${ty-10}, ${x} ${ty} Z`
      : `M${x} ${ty} C ${x-9} ${ty+10}, ${x-4} ${ty+22}, ${x} ${ty+26} C ${x+4} ${ty+22}, ${x+9} ${ty+10}, ${x} ${ty} Z`});
    // 잎 밑동(가지에 붙은 쪽)에서 자라나게 한다.
    p.style.transformOrigin = up ? '50% 100%' : '50% 0%';
    p.setAttribute('class', 'leafshape' + (leafOn(i) ? ' on' : ''));
    wrap.appendChild(p); g.appendChild(wrap); leaves.push(p);
    const s = svgEl('circle', {class:'spark', cx:x, cy:up ? ty - 14 : ty + 13, r:2});
    g.appendChild(s); sparks.push(s);
  });
}
function renderLeaves(){
  leaves.forEach((p, i) => {
    const on = leafOn(i);
    if (on && !p.classList.contains('on')) {
      p.classList.add('on');
      sparks[i].classList.remove('go'); void sparks[i].getBBox(); sparks[i].classList.add('go');
    } else if (!on) p.classList.remove('on');
  });
}

function shown(q){
  const v = state[q.key];
  if (q.type === 'choice') return v ? optLabel(q, v) : '';
  if (Array.isArray(v)) return v.join(q.key === 'sample_lines' ? ' / ' : ', ');
  return v || '';
}

function renderCard(){
  const n = QS.filter((q, i) => leafOn(i)).length;
  $('cm-name').textContent = state.name || T.noname;
  $('cm-sub').textContent = n === 0 ? T.seed : n === QS.length ? T.all_leaves : fill(T.leaves, {n});
  $('cm-list').innerHTML = QS.filter(q => q.key !== 'name').map(q => {
    const txt = shown(q);
    return `<div><dt>${esc(q.label)}</dt><dd class="${txt ? '' : 'empty'}">${txt ? esc(txt) : (q.required ? T.unset : T.none)}</dd></div>`;
  }).join('');
  renderLeaves();
}

function renderStep(){
  $('cm-result').classList.remove('show');
  const q = QS[step];
  if (q.key === 'relationship_ceiling' && state.relationship_ceiling && !ceilAllowed(state.relationship_ceiling)) {
    delete state.relationship_ceiling; save();
  }
  const v = state[q.key];
  let body = '';
  if (q.type === 'choice') {
    body = `<div class="cm-opts">${q.options.map(o => {
      const dis = q.key === 'relationship_ceiling' && !ceilAllowed(o.value);
      return `<button type="button" class="cm-opt" data-v="${esc(o.value)}" aria-pressed="${v === o.value}" ${dis ? `disabled title="${esc(T.ceil_disabled)}"` : ''}><strong>${esc(o.label)}</strong><span>${esc(o.desc || '')}</span></button>`;
    }).join('')}</div>`;
  } else {
    const multi = q.type === 'multi';
    const sel = Array.isArray(v) ? v : (v ? [v] : []);
    const customs = sel.filter(x => !q.options.includes(x));
    const full = multi && q.max_items && sel.length >= q.max_items;
    body = `<div class="cm-chips">${q.options.map(o =>
      `<button type="button" class="cm-chip" data-v="${esc(o)}" aria-pressed="${sel.includes(o)}">${esc(o)}</button>`).join('')}
      ${customs.map(c => `<button type="button" class="cm-chip custom" data-v="${esc(c)}" aria-pressed="true" title="${esc(T.remove_hint)}">${esc(c)}</button>`).join('')}</div>
      <div class="cm-custom"><input type="text" id="cm-in" placeholder="${esc(q.custom_label || '')}" aria-label="${esc(q.custom_label || q.label)}"${q.max_length ? ` maxlength="${q.max_length}"` : ''}>
      <button type="button" class="btn" id="cm-add">${esc(multi ? T.add : T.use_this)}</button></div>
      ${multi && q.max_items ? `<p class="cm-count">${sel.length} / ${q.max_items}${full ? esc(T.full) : ''}</p>` : ''}`;
  }
  $('cm-q').innerHTML = `<div class="cm-stepnum">${step + 1} / ${QS.length}${q.required ? '' : esc(T.optional)}</div>
    <h2>${esc(q.title)}</h2><p class="cm-hint">${esc(q.hint || '')}</p>${body}`;

  $('cm-q').querySelectorAll('[data-v]').forEach(b => b.addEventListener('click', () => pick(b.dataset.v)));
  const add = $('cm-add');
  if (add) {
    const go = () => { const t = $('cm-in').value.trim(); if (t) pick(q.max_length ? t.slice(0, q.max_length) : t, true); };
    add.addEventListener('click', go);
    $('cm-in').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
  }
  $('cm-prev').disabled = step === 0;
  const last = step === QS.length - 1;
  $('cm-next').textContent = last ? T.finish : (!q.required && !answered(q) ? T.skip : T.next);
  $('cm-next').disabled = q.required && !answered(q);
  renderCard();
}

function pick(val, isCustom){
  const q = QS[step];
  if (q.type === 'multi') {
    let arr = Array.isArray(state[q.key]) ? [...state[q.key]] : [];
    if (arr.includes(val)) { if (!isCustom) arr = arr.filter(x => x !== val); }
    else { if (q.max_items && arr.length >= q.max_items) arr.shift(); arr.push(val); }
    if (arr.length) state[q.key] = arr; else delete state[q.key];
  } else {
    if (state[q.key] === val && !isCustom) delete state[q.key]; else state[q.key] = val;
  }
  save();
  renderStep();
}

/* 결과 카드 — 설계서 5.1 모양. 칸 순서와 칸 이름은 서버가 보낸 card_keys를 따른다. */
function buildCardJson(){
  const card = {};
  S.card_keys.forEach(k => {
    const q = byKey[k];
    if (k === 'schema_version') card[k] = S.schema_version;
    else if (k === 'id') card[k] = null;               // 서버가 저장할 때 붙인다
    else if (k === 'expression_level') card[k] = null; // 이 서버는 수위를 정하지 않는다
    else if (k === 'promises') card[k] = S.promises.slice();
    else if (!q) card[k] = null;
    else if (q.type === 'multi') card[k] = Array.isArray(state[k]) ? state[k].slice() : [];
    else if (q.type === 'choice') card[k] = state[k] || null;
    else card[k] = state[k] || '';
  });
  return card;
}
function buildPreview(){
  const c = buildCardJson();
  const L = a => Array.isArray(a) ? a.join(', ') : (a || '');
  const lines = [`# ${c.name}${c.aliases && c.aliases.length ? fill(T.aliases, {msg: L(c.aliases)}) : ''}`, ''];
  QS.forEach(q => {
    if (q.key === 'name' || q.key === 'aliases' || q.key === 'sample_lines') return;
    lines.push(`${q.label}: ${shown(q)}`);
  });
  if (c.sample_lines && c.sample_lines.length) {
    lines.push('', T.sample_lines);
    c.sample_lines.forEach(s => lines.push(`- "${s}"`));
  }
  lines.push('', T.promise_head);
  c.promises.forEach(p => lines.push('- ' + p));
  lines.push('', T.persona_note);
  return lines.join('\n');
}

/* 저장한 뒤 서버가 돌려준 id·version을 기억해 둔다 — 같은 카드를 다시
   저장할 때 새 캐릭터로 오해되지 않고(이름 중복 거절) 고치는 것으로
   이어지게 하기 위해서다. */
function getSaved(){
  try { return JSON.parse(localStorage.getItem(SAVED_KEY) || 'null'); }
  catch(e) { return null; }
}
function setSaved(v){ try { localStorage.setItem(SAVED_KEY, JSON.stringify(v)); } catch(e) {} }
function buildSaveCard(){
  const card = buildCardJson();
  const saved = getSaved();
  if (saved && saved.id) card.id = saved.id;
  return card;
}

/* 저장 실패 문구. 한국어판은 서버가 준 문장을 그대로, 영어판은 오류 부호로 고른다. */
function errorText(data){
  data = data || {};
  if (data.error === 'invalid_card' && data.message) return fill(T.invalid_card, {msg: data.message});
  if (Object.keys(T.errors).length) return T.errors[data.error] || T.save_failed;
  return data.message || T.save_failed;
}

if (LOGGED_IN) {
  $('cm-save').hidden = false;
  $('cm-save').addEventListener('click', async () => {
    const card = buildSaveCard();
    const saved = getSaved();
    const btn = $('cm-save'), msg = $('cm-save-msg');
    btn.disabled = true;
    msg.className = 'cm-save-msg';
    msg.textContent = T.saving;
    try {
      const res = await fetch('/auth/character/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
        body: JSON.stringify({card, base_version: saved ? saved.version : null}),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        setSaved({id: data.id, version: data.version});
        msg.className = 'cm-save-msg ok';
        msg.textContent = fill(T.saved, {name: data.name});
        $('cm-connect-hint').hidden = false;
      } else {
        msg.className = 'cm-save-msg err';
        msg.textContent = errorText(data);
      }
    } catch(e) {
      msg.className = 'cm-save-msg err';
      msg.textContent = T.save_offline;
    } finally {
      btn.disabled = false;
    }
  });
} else {
  $('cm-save-login').hidden = false;
  /* 로그인하고 돌아오면 저장까지 이어서 하라는 표시 — 로그인 단추는 로그인만
     해 주므로, 표시가 없으면 저장을 누른 사람이 저장되지 않은 채 남는다. */
  $('cm-save-login').addEventListener('click', () => {
    try { localStorage.setItem(PENDING_KEY, '1'); } catch(e) {}
  });
}

let mode = 'json';
function showResult(){
  $('cm-out').textContent = mode === 'json' ? JSON.stringify(buildCardJson(), null, 2) : buildPreview();
  $('cm-tab-json').setAttribute('aria-pressed', mode === 'json');
  $('cm-tab-md').setAttribute('aria-pressed', mode === 'md');
  $('cm-result').classList.add('show');
  $('cm-result').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block:'start'});
}

$('cm-prev').addEventListener('click', () => { if (step > 0) { step--; renderStep(); } });
$('cm-next').addEventListener('click', () => {
  passed.add(step); savePassed();
  if (step < QS.length - 1) { step++; renderStep(); } else { renderCard(); showResult(); }
});
$('cm-tab-json').addEventListener('click', () => { mode = 'json'; showResult(); });
$('cm-tab-md').addEventListener('click', () => { mode = 'md'; showResult(); });
$('cm-copy').addEventListener('click', async () => {
  const text = $('cm-out').textContent;
  try { await navigator.clipboard.writeText(text); $('cm-toast').textContent = T.copied; }
  catch(e) {
    const r = document.createRange(); r.selectNodeContents($('cm-out'));
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    $('cm-toast').textContent = T.selected;
  }
  setTimeout(() => { $('cm-toast').textContent = ''; }, 2500);
});

$('cm-nav').hidden = false;
buildLeaves();
renderStep();

/* 로그인하고 저장하기를 눌러 로그인을 마치고 돌아왔다 — 결과 화면을 열고 바로 저장한다. */
let pending = null;
try { pending = localStorage.getItem(PENDING_KEY); localStorage.removeItem(PENDING_KEY); } catch(e) {}
if (pending && LOGGED_IN && QS.some(answered)) {
  renderCard(); showResult(); $('cm-save').click();
}
})();
</script>"""


def character_page(logged_in: bool = False, edit: dict | None = None, lang: str = "ko") -> str:
    t = _TEXT[lang]
    e = html.escape
    data = schema_data(lang)
    promises = "".join(f"<li>{e(p)}</li>" for p in data["promises"])
    edit_script = (
        f'<script type="application/json" id="cm-edit-data">{_json_for_script(edit)}</script>'
        if edit is not None
        else ""
    )
    body = (
        _CSS
        + '<header class="cm-head">'
        f'<span class="eyebrow">{e(t["eyebrow"])}</span>'
        f"<h1>{e(t['h1'])}</h1>"
        f'<p class="lead">{e(t["lead"])}</p>'
        "</header>"
        '<div class="cm-grid">'
        '<section class="cm-panel" aria-live="polite">'
        f'<div id="cm-q"><noscript><p>{e(t["noscript"])}</p></noscript></div>'
        '<div class="cm-nav" id="cm-nav" hidden>'
        f'<button type="button" class="btn" id="cm-prev">{e(t["prev"])}</button>'
        f'<button type="button" class="btn btn-primary" id="cm-next">{e(t["next"])}</button>'
        "</div>"
        '<div class="cm-result" id="cm-result">'
        f"<h2>{e(t['done_h2'])}</h2>"
        f'<p class="cm-hint">{e(t["done_hint"])}</p>'
        '<div class="cm-save">'
        f'<button type="button" class="btn btn-primary" id="cm-save" hidden>{e(t["save"])}</button>'
        '<a class="btn btn-primary" id="cm-save-login" '
        f'href="/auth/github/login?next={PATH_EN if lang == "en" else PATH}" hidden>'
        f"{e(t['save_login'])}</a>"
        '<span class="cm-save-msg" id="cm-save-msg" role="status"></span>'
        "</div>"
        '<p class="cm-connect-hint" id="cm-connect-hint" hidden>'
        f"{e(t['connect_hint'])} "
        f'<a href="/auth/me">{e(t["connect_link"])}</a> · '
        f'<a href="{ui.MY_CHARACTERS_PATH}">{e(t["mine_link"])}</a></p>'
        '<div class="cm-tabs">'
        '<button type="button" class="cm-chip" id="cm-tab-json" aria-pressed="true">JSON</button>'
        f'<button type="button" class="cm-chip" id="cm-tab-md" aria-pressed="false">{e(t["tab_md"])}</button>'
        f'<button type="button" class="btn" id="cm-copy">{e(t["copy"])}</button>'
        "</div>"
        '<pre class="cm-out" id="cm-out"></pre>'
        '<span class="cm-toast" id="cm-toast" role="status"></span>'
        "</div>"
        "</section>"
        f'<aside class="cm-card" aria-label="{e(t["preview_label"])}">'
        '<svg class="cm-twig" viewBox="0 0 320 84" aria-hidden="true">'
        '<path class="stem" d="M8 52 C 80 40, 160 60, 312 38"/>'
        '<g id="cm-leaves"></g>'
        "</svg>"
        f'<h3 id="cm-name">{e(t["js"]["noname"])}</h3>'
        f'<p class="cm-sub" id="cm-sub">{e(t["js"]["seed"])}</p>'
        '<dl id="cm-list"></dl>'
        f'<div class="cm-promise">{e(t["promise_head"])}'
        f"<ul>{promises}</ul></div>"
        "</aside>"
        "</div>"
        f'<script type="application/json" id="cm-schema">{_json_for_script(data)}</script>'
        + edit_script
        + _SCRIPT.replace("__DRAFT_KEY__", json.dumps(DRAFT_KEY))
        .replace("__LOGGED_IN__", "true" if logged_in else "false")
        .replace("__TEXT__", _json_for_script(t["js"]))
    )
    return ui.page(
        t["title"],
        f'<div class="wrap-wide" style="padding-top:36px">{body}</div>',
        current=PATH_EN if lang == "en" else PATH,
        cta="me" if logged_in else "start",
        description=t["description"],
        raw_body=True,
        lang=lang,
    )


def character_page_en(logged_in: bool = False) -> str:
    return character_page(logged_in, lang="en")


# 경로 → 그리는 함수. `web_auth._ALL_PUBLIC_PAGES`가 이 사전을 합쳐 라우트를 건다.
PAGES = {PATH: character_page, PATH_EN: character_page_en}
