"""캐릭터 만들기 공개 화면(`/character`) — 나무 캐릭터 설계서 0·11장.

여기서 지키는 것:
  1. 로그인 없이 열린다(문 목록과 라우트 양쪽).
  2. 질문은 화면에 손으로 적지 않고 코어 `character.schema()`에서 온다.
  3. 천장 규칙의 숫자도 코어 상수에서 온다.
  4. 사이트 밖으로 요청이 새지 않는다(프로토타입은 구글 글꼴을 불렀다).
  5. 영어판(`/en/character`)은 같은 틀에 보이는 글자만 바꾼다 — 코어에 질문이
     늘었는데 번역이 빠지면 여기서 걸린다.
"""
import html
import json
import re
import shutil
import subprocess

import pytest
from starlette.testclient import TestClient

import character_page
import routing_server as rs  # noqa: F401 — vendor/namu-plugin을 sys.path에 얹는다
import ui
import web_auth as wa


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_SESSION_SECRET", "test-session-secret-value")
    monkeypatch.setenv("NAMU_GITHUB_CLIENT_ID", "Iv1.testclientid")
    monkeypatch.setenv("NAMU_GITHUB_CLIENT_SECRET", "test-client-secret-marker-9f2a")
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))
    yield


def _page_data(out: str) -> dict:
    found = re.search(
        r'<script type="application/json" id="cm-schema">(.*?)</script>', out, re.S
    )
    assert found, "질문 데이터가 화면에 실려 있지 않다"
    return json.loads(found.group(1))


def test_character_page_opens_without_login_and_carries_every_core_question():
    import character

    client = TestClient(wa.build_auth_app(), base_url="https://testserver")

    r = client.get("/character")

    assert r.status_code == 200
    data = _page_data(r.text)
    assert len(data["questions"]) == len(character.QUESTIONS) == 11


def test_character_path_is_a_public_door():
    assert "/character" in ui.PUBLIC_PATHS
    assert "/character" in rs._WEB_PATHS


def test_questions_and_rules_come_from_the_core():
    import character

    data = _page_data(character_page.character_page(False))

    assert data["questions"] == character.schema()["questions"]
    assert data["promises"] == list(character.PROMISES)
    assert data["ceiling_rank"] == {c["value"]: c["rank"] for c in character.CEILINGS}
    assert data["start_min_ceiling_rank"] == character.START_MIN_CEILING_RANK
    assert set(data["card_keys"]) == set(character.CARD_KEYS)
    # 카드 칸 순서는 설계서 5.1(스키마 예시)과 같다.
    assert data["card_keys"] == list(character.schema()["example"])


def test_page_has_no_hand_written_question_list_or_outside_fonts():
    out = character_page.character_page(False)

    for leftover in ("START_CODE", "CEIL_CODE", "STEPS", "Gowun", "googleapis", "gstatic"):
        assert leftover not in out
    assert "</script>" not in _page_data(out).__repr__()


def test_page_links_only_to_known_places():
    import pages
    import pages_en

    known = set(pages.PAGES) | set(pages_en.PAGES) | set(character_page.PAGES)
    known |= {"/auth/github/login", "/auth/me", ui.MY_CHARACTERS_PATH} | set(ui.ICON_PATHS)
    out = character_page.character_page(False)

    for href in re.findall(r'href="(/[^"]*)"', out):
        assert href.split("?")[0].split("#")[0] in known, href


# ---------------------------------------------------------------------------
# 위쪽 메뉴와 영어판
# ---------------------------------------------------------------------------
_HANGUL = re.compile(r"[가-힣]")


def test_character_is_in_both_menus_and_the_language_button_pairs_them():
    ko = character_page.character_page(False)
    en = character_page.character_page_en(False)

    assert '<a href="/character"' in ko and '<a href="/en/character"' in en
    assert ui.LANG_PAIRS["/character"] == "/en/character"
    assert 'href="/en/character"' in ko  # 한국어 화면의 언어 단추
    assert 'href="/character"' in en  # 영어 화면의 언어 단추
    assert '<html lang="en"' in en


def test_english_page_opens_without_login():
    client = TestClient(wa.build_auth_app(), base_url="https://testserver")

    r = client.get("/en/character")

    assert r.status_code == 200
    assert "/en/character" in rs._WEB_PATHS
    assert len(_page_data(r.text)["questions"]) == 11


def test_english_questions_keep_every_value_and_limit_of_the_core():
    """번역은 보이는 글자만 바꾼다. 칸 이름·고르기 값·상한이 달라지면 영어판에서
    만든 카드가 저장 검사에서 엉뚱하게 거절된다."""
    import character

    core = character.schema()["questions"]
    en = _page_data(character_page.character_page_en(False))["questions"]

    assert [q["key"] for q in en] == [q["key"] for q in core]
    shown = {"label", "title", "hint", "custom_label", "options"}
    for c, e in zip(core, en):
        assert {k: v for k, v in c.items() if k not in shown} == {
            k: v for k, v in e.items() if k not in shown
        }, c["key"]
        assert len(e["options"]) == len(c["options"]), c["key"]
        if c["type"] == "choice":
            assert [o["value"] for o in e["options"]] == [o["value"] for o in c["options"]]


def test_english_page_shows_no_korean_text():
    """질문·약속·스크립트가 쓰는 문구에 한국어가 남으면 영어 방문자에게 그대로 보인다.
    스크립트 안의 설명 주석과 언어 단추(`한국어`)는 보이는 글자가 아니므로 뺀다."""
    out = character_page.character_page_en(False)
    data = _page_data(out)

    assert not _HANGUL.search(json.dumps(data, ensure_ascii=False))
    assert not _HANGUL.search(json.dumps(character_page._TEXT["en"], ensure_ascii=False))
    visible = re.sub(r"<script.*?</script>|<style.*?</style>", "", out, flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", visible).replace("한국어", "")
    assert not _HANGUL.search(visible), _HANGUL.search(visible)


def test_both_languages_have_the_same_screen_text_keys():
    ko, en = character_page._TEXT["ko"], character_page._TEXT["en"]

    assert set(ko) == set(en)
    assert set(ko["js"]) == set(en["js"])


def test_english_save_errors_cover_every_error_code_of_the_save_route():
    """영어판은 서버의 한국어 문장 대신 오류 부호로 문구를 고른다. 저장 주소에
    부호가 늘었는데 여기 없으면 영어 방문자는 뭉뚱그린 문구만 본다."""
    import inspect

    codes = set(re.findall(r'"error": "(\w+)"', inspect.getsource(wa._character_save_sync)))
    codes.discard("invalid_card")  # 따로 다룬다(`invalid_card` 문구)

    assert codes == set(character_page._TEXT["en"]["js"]["errors"])


def test_hidden_attribute_beats_button_display():
    # .btn{display:inline-flex}가 hidden을 이겨 로그인 전·후 저장 단추가 둘 다
    # 보이던 사고(2026-10-09). 숨김 규칙이 빠지면 같은 일이 다시 난다.
    page = character_page.character_page(False)
    assert "[hidden]{display:none !important;}" in page
    assert 'id="cm-save" hidden' in page
    assert 'id="cm-save-login"' in page


def test_new_character_keeps_changed_draft_and_saved_marker_carries_name():
    # 저장 표시만 보고 만들던 초안(아인)을 지운 사고(2026-10-09) — 저장 그대로인
    # 초안만 비우고, 저장 표시에는 이름을 실어 다른 캐릭터를 덮어쓰지 않는다.
    page = character_page.character_page(True)
    assert "rec.snap === now" in page
    assert "if (v.name !== state.name && !editEl) return null;" in page
    assert "snap: JSON.stringify(state)" in page
    assert "T.pending_empty" in page and "T.pending_no_login" in page


# ---------------------------------------------------------------------------
# 고치기 화면은 질문이 아닌 칸(대표사진·감정별 사진)을 원래 값 그대로 이어 싣는다.
# 질문 칸만 남기는 `clean()`을 거친 뒤 `!q` 칸을 null로 보내면, 사진을 단 캐릭터를
# 웹에서 한 번 고치기만 해도 portrait·emotion_photos가 지워진다(2026-10-10 검수).
# ---------------------------------------------------------------------------
def test_edit_page_keeps_non_question_fields_in_the_script():
    page = character_page.character_page(True, edit={"id": None, "version": None, "card": {}})
    assert "const KEEP = {};" in page
    assert "if (!byKey[k] && !FIXED_KEYS.includes(k) && k in pc) KEEP[k] = pc[k];" in page
    assert "else if (!q) card[k] = k in keep ? keep[k] : null;" in page
    # 고정 칸(id·약속 등)은 이어 싣지 않는다 — 서버가 정하는 값이다.
    assert "const FIXED_KEYS = ['schema_version', 'id', 'expression_level', 'promises'];" in page


# 화면 스크립트를 실제로 node에서 돌려 저장 단추가 보내는 카드를 받아 본다.
# DOM은 저장에 필요한 만큼만 흉내 낸다(무엇을 물어도 받아 주는 가짜 요소).
_NODE_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const pages = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let inp = null;
function el(){
  const h = {};
  return {textContent:'', innerHTML:'', hidden:false, disabled:false, className:'', value:'',
    style:{}, dataset:{}, classList:{add(){}, remove(){}, contains(){ return false; }},
    addEventListener(t, f){ h[t] = f; }, click(){ return h.click && h.click(); },
    appendChild(){}, setAttribute(){}, querySelectorAll(){ return []; },
    getBBox(){ return {}; }, scrollIntoView(){}};
}
let els = {};
const store = {};
globalThis.document = {
  getElementById(id){
    if (id === 'cm-schema') return {textContent: inp.schema};
    if (id === 'cm-edit-data') return inp.edit === null ? null : {textContent: inp.edit};
    return els[id] || (els[id] = el());
  },
  createElementNS(){ return el(); },
};
globalThis.localStorage = {getItem: k => k in store ? store[k] : null,
  setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; }};
globalThis.location = {search: '', pathname: '/character'};
globalThis.history = {replaceState(){}};
globalThis.matchMedia = () => ({matches: true});
let sent = null;
globalThis.fetch = async (url, opt) => { sent = JSON.parse(opt.body);
  return {ok: true, json: async () => ({ok: true, id: 'x', version: 'v', name: 'n'})}; };
// 같은 브라우저(같은 localStorage)에서 화면을 차례로 연다 — 저장 단추는 마지막 화면 것을 누른다.
pages.forEach(p => { inp = p; els = {}; vm.runInThisContext(p.script); });
(async () => { await els['cm-save'].click(); process.stdout.write(JSON.stringify(sent)); })();
"""


def _page_parts(page: str) -> dict:
    schema = re.search(r'<script type="application/json" id="cm-schema">(.*?)</script>', page, re.S)
    edit = re.search(r'<script type="application/json" id="cm-edit-data">(.*?)</script>', page, re.S)
    script = re.search(r"<script>\s*(\(function\(\)\{\s*const S = JSON\.parse.*?)</script>", page, re.S)
    assert schema and script
    return {
        "schema": schema.group(1),
        "edit": edit.group(1) if edit else None,
        "script": script.group(1),
    }


def _run_save_in_node(tmp_path, *pages: str, harness_text: str = _NODE_HARNESS) -> dict:
    """화면들을 한 브라우저에서 차례로 열고, 마지막 화면의 저장 단추가 보낸 것을 돌려준다."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node가 없어 화면 스크립트를 돌릴 수 없다")
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps([_page_parts(p) for p in pages]), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(harness_text, encoding="utf-8")
    out = subprocess.run([node, str(harness), str(inp)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_editing_a_card_with_photos_keeps_portrait_and_emotion_photos(tmp_path):
    keys = character_page.schema_data()["card_keys"]
    if "portrait" not in keys or "emotion_photos" not in keys:
        pytest.skip("빌려 쓰는 본체 판에 아직 사진 칸이 없다")
    card = {
        "schema_version": 1, "id": None, "name": "하린", "aliases": [],
        "relationship_start": "stranger", "relationship_ceiling": "open",
        "portrait": "attach_file/portrait.png",
        "emotion_photos": {"기쁨": "attach_file/happy.png", "슬픔": "attach_file/sad.png"},
    }
    page = character_page.character_page(
        True, edit={"id": "01JABCDEFGHJKMNPQRSTVWXYZ0", "version": "v1", "card": card})
    sent = _run_save_in_node(tmp_path, page)
    assert sent["card"]["portrait"] == "attach_file/portrait.png"
    assert sent["card"]["emotion_photos"] == {"기쁨": "attach_file/happy.png",
                                              "슬픔": "attach_file/sad.png"}
    assert sent["card"]["id"] == "01JABCDEFGHJKMNPQRSTVWXYZ0"
    assert sent["card"]["name"] == "하린"


def test_create_page_after_edit_page_keeps_photos_of_the_same_character(tmp_path):
    # 고치기 화면이 남긴 표시(id·판 번호)를 같은 브라우저의 만들기 화면(/character)이
    # 이어받아 같은 캐릭터를 고치는 저장이 된다 — 이때 사진 칸도 같은 표시에서 이어
    # 실어야 한다. 아니면 null로 나가고 판 번호가 맞아 서버가 받아 사진이 지워진다.
    keys = character_page.schema_data()["card_keys"]
    if "portrait" not in keys or "emotion_photos" not in keys:
        pytest.skip("빌려 쓰는 본체 판에 아직 사진 칸이 없다")
    card = {
        "schema_version": 1, "id": None, "name": "하린", "aliases": [],
        "relationship_start": "stranger", "relationship_ceiling": "open",
        "portrait": "attach_file/portrait.png",
        "emotion_photos": {"기쁨": "attach_file/happy.png"},
    }
    edit_page = character_page.character_page(
        True, edit={"id": "01JABCDEFGHJKMNPQRSTVWXYZ0", "version": "v1", "card": card})
    create_page = character_page.character_page(True)
    sent = _run_save_in_node(tmp_path, edit_page, create_page)
    assert sent["card"]["id"] == "01JABCDEFGHJKMNPQRSTVWXYZ0"
    assert sent["base_version"] == "v1"
    assert sent["card"]["portrait"] == "attach_file/portrait.png"
    assert sent["card"]["emotion_photos"] == {"기쁨": "attach_file/happy.png"}


# ---------------------------------------------------------------------------
# 캐릭터별 규칙(`custom_rules`) — 코어에 새로 생긴 질문 칸. 다른 여러 줄 칸(예시
# 대사)처럼 화면에 나오고, 질문 칸 경로(`state`)로 실려 고치기에서 이어진다.
# 질문이 아닌 칸을 잇는 `KEEP`에 섞이면 고친 값이 원래 값으로 되돌아간다.
# ---------------------------------------------------------------------------
_RULES = ["내가 힘들다고 하면 먼저 쉬자고 말해 줘", "밤 열두 시가 넘으면 자라고 해 줘"]


def _rules_question(page: str) -> dict:
    found = [q for q in _page_data(page)["questions"] if q["key"] == "custom_rules"]
    assert len(found) == 1, "custom_rules 질문이 화면에 없다"
    return found[0]


def test_custom_rules_question_is_on_both_pages_after_sample_lines():
    ko = _rules_question(character_page.character_page(False))
    en = _rules_question(character_page.character_page_en(False))

    assert ko["label"] == "지키는 규칙" and ko["type"] == "multi"
    assert (ko["max_items"], ko["max_length"], ko["required"]) == (5, 100, False)
    assert en["label"] == "Rules it keeps"
    for k in ("label", "title", "hint", "custom_label"):
        assert not _HANGUL.search(en[k]), (k, en[k])
    assert {k: v for k, v in en.items() if k not in ("label", "title", "hint", "custom_label")} == {
        k: v for k, v in ko.items() if k not in ("label", "title", "hint", "custom_label")
    }
    keys = [q["key"] for q in _page_data(character_page.character_page(False))["questions"]]
    assert keys.index("custom_rules") == keys.index("sample_lines") + 1
    assert keys.index("custom_rules") + 1 == keys.index("relationship_ceiling")


def test_missing_english_translation_fails_loudly(monkeypatch):
    en = dict(character_page._QUESTIONS_EN)
    en.pop("custom_rules")
    monkeypatch.setattr(character_page, "_QUESTIONS_EN", en)
    with pytest.raises(KeyError):
        character_page.schema_data("en")


def _edit_card_with_rules() -> dict:
    return {
        "schema_version": 1, "id": None, "name": "하린", "aliases": [],
        "relationship_start": "stranger", "relationship_ceiling": "open",
        "custom_rules": list(_RULES),
    }


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_editing_keeps_custom_rules_as_a_question_field(tmp_path, lang):
    page = character_page.character_page(
        True, edit={"id": "01JABCDEFGHJKMNPQRSTVWXYZ0", "version": "v1",
                    "card": _edit_card_with_rules()}, lang=lang)
    sent = _run_save_in_node(tmp_path, page)
    assert sent["card"]["custom_rules"] == _RULES
    assert sent["card"]["name"] == "하린"


# 고치기 화면에서 규칙 하나를 더 써넣고 저장한다 — 미리 채운 값이 초안(state)에
# 있어야 거기에 이어 붙고, KEEP으로 갔다면 고친 값이 아니라 원래 값이 나간다.
_NODE_ADD_RULE = _NODE_HARNESS.replace(
    "(async () => { await els['cm-save'].click();",
    "(async () => {\n"
    "  const S = JSON.parse(inp.schema);\n"
    "  const at = S.questions.findIndex(q => q.key === 'custom_rules');\n"
    "  for (let i = 0; i < at; i++) els['cm-next'].click();\n"
    "  els['cm-in'].value = '새로 더한 규칙';\n"
    "  els['cm-add'].click();\n"
    "  els['cm-tab-md'].click();\n"
    "  const preview = els['cm-out'].textContent;\n"
    "  await els['cm-save'].click(); sent.preview = preview;",
)


def test_editing_adds_a_rule_on_top_of_the_prefilled_ones_and_shows_it_in_preview(tmp_path):
    assert _NODE_ADD_RULE != _NODE_HARNESS
    page = character_page.character_page(
        True, edit={"id": "01JABCDEFGHJKMNPQRSTVWXYZ0", "version": "v1",
                    "card": _edit_card_with_rules()})
    sent = _run_save_in_node(tmp_path, page, harness_text=_NODE_ADD_RULE)
    assert sent["card"]["custom_rules"] == _RULES + ["새로 더한 규칙"]
    preview = sent["preview"]
    # 미리보기 글에서는 고정 약속 뒤에 규칙 목록으로 보인다(코어 persona_text와 같은 순서).
    assert preview.index("언제나 지키는 약속") < preview.index("지키는 규칙\n- 내가 힘들다고")
    assert "- 새로 더한 규칙" in preview
    # 질문 목록 줄에는 다시 나오지 않는다(예시 대사처럼 따로 묶는다).
    assert "지키는 규칙: " not in preview


def test_new_card_sends_no_photos(tmp_path):
    # 새로 만들기에는 이어 실을 원래 카드가 없다 — 사진 칸은 비워 보낸다(본체가 기본값을 채운다).
    page = character_page.character_page(True)
    sent = _run_save_in_node(tmp_path, page)
    for k in ("portrait", "emotion_photos"):
        assert sent["card"].get(k) is None


# ---------------------------------------------------------------------------
# AI 연결 안내(설계서 10장·14장 5단계) — `connect_texts`가 만드는 세 글.
# ---------------------------------------------------------------------------
_TOOLS = ("namu_character_load", "namu_record(bowl=profile)", "namu_character_diary",
          "namu_character_core", "archive")


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_connect_texts_carry_the_five_rules_with_real_tool_names(lang):
    t = character_page.connect_texts("하린", lang)

    assert t["command_path"] == ".claude/commands/하린.md"
    for key in ("project", "claude_md", "command"):
        assert 'namu_character_load(name="하린")' in t[key], key
        for tool in _TOOLS:
            assert tool in t[key], (key, tool)
        # 규칙 다섯 줄이 번호와 함께 들어 있다.
        assert all(f"\n{i}. " in t[key] for i in range(1, 6)), key
    assert t["command"].startswith("---\ndescription: ")
    # 설계서의 기능 이름(load·diary)만 적힌 줄이 남지 않았다.
    assert "(load" not in t["project"] and "(diary" not in t["project"]


def test_connect_texts_english_has_no_korean_except_the_name():
    t = character_page.connect_texts("Harin", "en")
    assert not _HANGUL.search(json.dumps(t, ensure_ascii=False))
    assert not _HANGUL.search(json.dumps(character_page._CONNECT_UI["en"], ensure_ascii=False))
    assert set(character_page._CONNECT_UI["ko"]) == set(character_page._CONNECT_UI["en"])


def test_connect_texts_survive_quotes_and_odd_names():
    t = character_page.connect_texts('하"린 </script>\n둘')

    # 줄바꿈은 빈칸 하나로 접는다 — 지침의 줄 구조가 깨지지 않게.
    assert t["name"] == '하"린 </script> 둘'
    # 도구 인자 자리는 JSON 문자열이라 따옴표가 끊기지 않는다.
    assert 'namu_character_load(name="하\\"린 </script> 둘")' in t["project"]
    # 머리말의 description은 JSON(=YAML 큰따옴표) 문자열로 읽힌다.
    desc = t["command"].split("\n")[1].removeprefix("description: ")
    assert json.loads(desc).endswith('하"린 </script> 둘')
    # 파일 이름에는 경로를 끊는 글자가 들어가지 않는다.
    fname = t["command_path"].removeprefix(".claude/commands/")
    assert "/" not in fname and '"' not in fname and " " not in fname
    assert character_page.connect_texts("///")["command_path"] == ".claude/commands/character.md"


def test_connect_block_escapes_the_texts_into_html():
    texts = character_page.connect_texts('<b>"악"</b>')
    block = character_page.connect_block_html("cc-0", texts)

    assert "<b>" not in block
    assert "&lt;b&gt;" in block
    assert 'id="cc-0-project"' in block and 'id="cc-0-command-path"' in block
    assert block.count('class="btn cc-copy"') == 3


@pytest.mark.parametrize("name, eul, gwa, iga", [
    ("아인", "을", "과", "이"),      # 받침 있음
    ("미미", "를", "와", "가"),      # 받침 없음
    ("Rua", "을(를)", "과(와)", "이(가)"),  # 한글이 아니면 둘을 함께
    ("", "을(를)", "과(와)", "이(가)"),
])
def test_josa_follows_the_final_consonant(name, eul, gwa, iga):
    assert character_page._josa(name, "을", "를") == eul
    assert character_page._josa(name, "과", "와") == gwa
    assert character_page._josa(name, "이", "가") == iga


def test_connect_texts_intro_uses_the_right_particles():
    a = character_page.connect_texts("아인")
    assert a["intro"] == (
        '나무가 연결된 AI에게 "나무를 통해서 아인을 불러 줘" 또는 '
        '"나무를 통해서 아인 캐릭터를 불러와 줘"라고 말해 보세요. 바로 아인과 대화할 수 있어요.'
    )
    assert a["more_lead"].startswith("아래 지침이나 파일을 한 번 설정해 두면, 대화를 열 때 아인이 ")
    assert a["more_lead"].endswith("지침대로 아인과 대화할 수 있어요.")
    m = character_page.connect_texts("미미")
    assert "미미를 불러 줘" in m["intro"] and "미미와 대화할" in m["intro"]
    assert "미미가 자동으로" in m["more_lead"] and "미미와 대화할" in m["more_lead"]
    assert "Rua을(를) 불러 줘" in character_page.connect_texts("Rua")["intro"]
    en = character_page.connect_texts("Rua", "en")
    assert en["intro"] == (
        'Tell an AI connected to Namu "Load Rua through Namu" or "Bring in the Rua '
        'character through Namu". You can start talking with Rua right away.'
    )
    assert en["more_lead"].startswith("Set one of these up once, and Rua loads")


def test_connect_block_keeps_the_three_texts_folded_behind_the_intro():
    block = character_page.connect_block_html("cc-0", character_page.connect_texts("아인"))

    intro_at = block.index('id="cc-0-intro"')
    fold_at = block.index('<details class="cc-more"><summary>더 편하게 쓰기 (선택)</summary>')
    # 기본 안내는 접힘 바깥(앞)에, 세 글과 펼친 안내는 모두 접힘 안에 있다.
    assert intro_at < fold_at
    for key in ("more-lead", "project", "claude-md", "command", "command-path"):
        assert block.index(f'id="cc-0-{key}"') > fold_at, key
    assert 'class="cc-more" open' not in block  # 기본은 접힘
    assert "<h3>대화하는 법</h3>" in block
    assert html.escape("오른쪽 '지침 → 편집'에 붙여 넣고 저장하세요.") in block
    en = character_page.connect_block_html("x", lang="en")
    assert "<summary>Make it easier (optional)</summary>" in en and "<h3>How to talk</h3>" in en


def test_character_page_has_a_hidden_connect_block_filled_from_the_save_response():
    for lang, page in (("ko", character_page.character_page(True)),
                       ("en", character_page.character_page_en(True))):
        assert 'class="cc-block" id="cm-connect"' in page
        assert re.search(r'id="cm-connect"[^>]*hidden', page), lang
        for key in ("intro", "more-lead", "project", "claude-md", "command", "command-path"):
            assert f'id="cm-connect-{key}"' in page, (lang, key)
        assert f"const LANG = {json.dumps(lang)};" in page
        assert "lang: LANG" in page
        assert "data.connect" in page
        # 복사 실패 때는 cm-copy처럼 글을 선택해 둔다.
        assert "selectNodeContents(src)" in page
