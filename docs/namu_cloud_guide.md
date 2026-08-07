# NAMU 공용 클라우드 MCP 사용 가이드 (경로 A — 중앙 호스팅·멀티유저)

> 🌳 **처음 오셨다면 이 문서가 아니라 <https://namu-cloud.onnamu.kr/> 로 가세요.**
> 가입 절차는 사이트 화면이 안내합니다. 이 문서는 그 뒤에 "안에서 어떻게
> 도는가"를 알고 싶은 사람을 위한 것입니다.

> 📅 2026-08-08 개정(첨부 파일·다섯 그릇 검색 반영 — 1절·5절 끝) · 2026-08-02 개정(namu-70, 사이트 신설로 2-2절 교체) · 2026-07-31 개정(namu-60)
> · 최초 작성 2026-07-19(namu-54) · 선행 문서: [`remote_mcp_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_guide.md)(경로 B 셀프호스팅 가이드) · [`install_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/install_guide.md)(플러그인 설치 가이드) · [`remote_mcp_design.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_design.md)(설계 원본).
>
> **범위** — 경로 A는 "중앙에서 우리가 대신 호스팅해주는 공용 서버에 접속만 하면 되는" 형태다(사용자가 직접 서버를 띄우는 경로 B와 반대).
>
> **이 문서의 개정 이력(중요)** — 2026-07-19 최초본은 ⓐ 접속 주소가 전원 공용 시크릿 + `?user=<내 키>` 형식이고 ⓑ 그 주소를 로컬 Claude Code에도 `claude mcp add`로 붙이라고 안내했다. **둘 다 지금은 틀린 설명이다.** ⓐ는 namu-59에서 사용자별 개인 열쇠로 바뀌었고(전원 공용 열쇠는 인증이 아니었다 — 5절), ⓑ는 애초에 잘못된 안내였다(2절). 옛 문서를 보고 설정한 사용자는 2절을 다시 읽고 고쳐 잡을 것.

## 1. 이게 뭔가 — 경로 B(셀프호스팅)와 차이

| | 경로 B(셀프호스팅) | 경로 A(공용 클라우드, 이 문서) |
|---|---|---|
| 서버는 누가 띄우나 | 사용자 본인이 직접 | 중앙(onnamu.kr)이 대신 호스팅 |
| 사용자 수 | 단일 사용자(자기 것 하나) | 멀티유저(주소에 실린 **개인 열쇠**로 사용자별 서랍 라우팅) |
| 기억의 원본은 어디에 | 자기 PC의 `~/.namu` | **사용자 본인의 GitHub 저장소**(연결 시 직접 고른다). 서버가 갖는 것은 그 저장소의 사본뿐이다 |

노출 도구는 개인용(경로 B)과 동일한 **10종**이다 — 기억 3종(`namu_recall`/`namu_record`/`namu_search`)과 첨부 7종(`namu_upload_file`/`namu_list_files`/`namu_download_file`/`namu_delete_file`/`namu_create_upload_ticket`/`namu_create_download_ticket`/`namu_check_ticket`). 기록은 개인용과 같은 3층(요약 `summary` · 왜 `reason` · 원문 `body`)으로 남는다(namu-68).

담을 수 있는 그릇은 **다섯 전부**다 — 교훈(learnings)·개인 사실(profile)·작업일지(tasks)·쪽지(memo)·첨부 기록(attachments). 노출되지 않는 것은 `namu_sync_setup`(서버의 저장소 배선을 바꾸는 도구라 원격에 열면 remote 탈취로 이어진다)과 쪽지 떼기·책갈피 2종(그 PC의 파일을 다루는 도구)뿐이다. 경로 B와 같은 기준이다 — [`remote_mcp_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_guide.md) 1절 참고.

> **개정 이력** — 최초본은 "그릇 셋(교훈·개인 사실·쪽지), tasks는 노출되지 않는다"고 적었다. 지금은 틀린 설명이다. 작업일지는 `routing_server.namu_record`의 `bowl == "tasks"` 분기로 기록되며(기록할 때 어느 프로젝트인지 함께 적어야 한다 — 웹에는 "지금 열어 둔 폴더"가 없기 때문), 첨부 기록 그릇은 `namu-file-upload-download`(2026-08-07)로 신설됐다.

포트·이미지 태그·Cloudflare ingress 같은 인프라 세부는 onnamu-project/specs 관할이라 이 문서에서는 다루지 않는다(중복 관리 금지).

## 2. 연결법 — 이 주소는 **웹 AI 전용**이다

### 2-1. 먼저 알아야 할 것 (여기서 갈린다)

| 무엇을 쓰시나요 | 어떻게 하나요 |
|---|---|
| **웹 AI**(claude.ai, ChatGPT 등 브라우저에서 쓰는 AI) | 이 문서대로 **접속 주소를 커넥터에 붙인다** |
| **Claude Code · agy**(터미널에서 쓰는 AI) | 주소를 붙이는 게 아니라 **나무를 플러그인으로 설치**한다 → [`install_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/install_guide.md) |

왜 Claude Code·agy는 주소를 붙이면 안 되나 — 이 주소로 넘어가는 것은 **기억과 파일뿐**이다. 세션 시작 브리핑, `/namu-task` 작업 절차, 워커 호출, 마무리 훅처럼 나무의 나머지 절반은 플러그인에만 들어 있고 주소로는 따라오지 않는다. 게다가 기억이 쌓이는 자리도 갈라진다(플러그인은 그 PC의 `~/.namu`, 이 주소는 사용자 GitHub 저장소). 반쪽짜리 나무를 쓰면서 기억까지 두 곳으로 흩어지는 셈이라, 터미널 사용자에게는 권하지 않는다.

### 2-2. 접속 주소 받기 — 절차는 사이트가 안내한다

**<https://namu-cloud.onnamu.kr/> 에서 시작하면 된다.** 화면이 시키는 대로
따라가면 로그인 → 저장소 마련 → 권한 → 접속 주소까지 끝난다. 창을 닫았어도
<https://namu-cloud.onnamu.kr/auth/me>(내 페이지)에서 주소를 다시 볼 수 있다.

> **왜 여기 절차를 다시 적지 않나** (namu-70) — 화면과 문서 두 곳에 같은 절차가
> 있으면 화면을 고칠 때 한쪽만 고쳐진다. 이 프로젝트에서 반복된 사고 양식이라,
> **절차의 원본은 화면 자체**로 두고 문서는 사이트를 가리키기만 한다.
> 사이트 안의 [시작하기](https://namu-cloud.onnamu.kr/start) 페이지가 그 원본이다.

| 사이트 페이지 | 무엇이 있나 |
|---|---|
| [홈](https://namu-cloud.onnamu.kr/) | 한 줄 소개 · 기억이 어디 있는지 · 웹/터미널 갈림길 |
| [시작하기](https://namu-cloud.onnamu.kr/start) | 가입부터 AI에 붙이기까지 네 걸음 |
| [무엇을 기억하나](https://namu-cloud.onnamu.kr/memory) | 3층 기록 · 그릇 다섯 가지 · 파일 주고받기 |
| [안전](https://namu-cloud.onnamu.kr/safety) | 원본 위치 · 권한 범위 · 주소 관리 · 그만두는 법 |
| [자주 묻는 질문](https://namu-cloud.onnamu.kr/faq) | 요금 · 저장소 · 여러 AI 연결 등 |

주소 형식은 다음과 같다.

```
https://namu-cloud.onnamu.kr/mcp/<내-개인-열쇠>?client=<AI-이름>
```

- `<내-개인-열쇠>` — 사람마다 다른 무작위 값(namu-59). 위 절차로 자동 발급되며, 운영자가 따로 나눠주는 공용 값이 아니다.
- `?client=` — 이 기억을 남긴 AI가 누구인지 나타내는 출처 태그(3절).

> ⚠️ **이 주소는 사실상 비밀번호다.** 아는 사람은 회원님 기억을 읽고 쓸 수 있다 — 채팅·이슈·스크린샷에 그대로 붙여넣지 말 것. 새어 나갔다면 내 페이지에서 **재발급**하면 옛 주소는 즉시 막힌다(5절).

### 2-3. claude.ai에 붙이기

설정(Settings) → 커넥터(Connectors) → 사용자 정의 커넥터 추가(Add custom connector) → 위 주소를 통째로 붙여넣고 저장.

다른 웹 AI를 쓴다면 주소 끝의 `client=claude`를 그 AI 이름으로 바꿔서 붙인다(예: `client=chatgpt`).

## 3. `client`가 뭔가

- **`client`** — 이 기억을 남긴 AI가 누구인지 나타내는 출처 태그(내부적으로 `via`로 저장). record 시 함께 저장돼, 나중에 "claude가 남긴 이력만" 같은 출처별 조회의 기준이 된다.
- **`user`는 이제 사용자가 붙이지 않는다.** 예전에는 `?user=<내 키>`로 서랍 이름표를 직접 적어 보냈지만, 지금은 서버가 주소에 실린 개인 열쇠로 누구인지 판정해 **직접 채워 넣는다**(요청에 `?user=`를 손으로 실어 보내도 서버가 걷어내고 다시 쓴다). 남의 이름표를 적어 넣어 남의 서랍을 여는 수법을 없애기 위한 구조다.

입력 규칙(실코드로 검증된 형식):

- `client` 값 — 영숫자·`.`·`_`·`-` 1~40자(`^[A-Za-z0-9._-]{1,40}$`).
- **3개 도구(recall/record/search) 모두 `client`가 필수**다 — 없거나 형식이 틀리면 도구 호출이 한국어+영어 상세 에러로 거부된다(주소 끝에 `?client=<AI 이름>`을 붙여 다시 등록하면 된다).

`client` 이름은 **정확한 모델명 예시(`claude` / `chatgpt` / `gemini` / `cursor` / `copilot`)로 넣기를 권장**한다. 애칭·변형도 형식만 맞으면 거부되지 않지만, **나중에 조회할 때 저장했던 값과 글자 그대로 똑같이 넣어야 찾힌다** — `claude`와 `cld`는 서로 다른 값으로 저장된다.

## 4. 저장소를 아직 연결하지 않았다면

로그인만 하고 저장소를 고르지 않은 상태에서는 도구 호출이 "먼저 로그인하고 저장소를 연결하라"는 안내와 함께 거부된다(빈 기억이 조용히 만들어지지 않는다). 2-2절 절차를 끝까지 밟을 것.

## 5. 인증·격리 모델 (현재)

- **인증은 주소에 실린 개인 열쇠 하나**다(namu-59). 서버는 요청마다 그 열쇠를 장부에서 찾아 "누구의 서랍인가"를 판정하고, 못 찾으면 404로 끊는다.
- 따라서 **주소를 아는 것 = 그 사람 본인으로 인정받는 것**이다. 남에게 알려주지 말 것.
- **주소가 새어 나갔거나 그만 쓰고 싶다면** — 내 페이지(<https://namu-cloud.onnamu.kr/auth/me>)에서 직접 처리한다(namu-60).
  - **연결 시험** — 지금 이 주소가 실제로 동작하는지 서버가 대신 확인해 준다. 판정은 `살아있음` / `주소가 잘못됨` / `지금은 확인 불가` 셋이며, 마지막은 **주소가 잘못됐다는 뜻이 아니다**(서버 재시작 직후 등 일시적 상황) — 1~2분 뒤 다시 눌러보면 된다.
  - **재발급** — 새 주소를 만든다. 옛 주소는 그 순간 막히므로, AI에 등록해 둔 커넥터 주소도 새것으로 바꿔야 한다.
  - **폐기** — 주소를 아예 없앤다(확인 단계를 한 번 거친다). 기억 자체는 회원님 GitHub 저장소에 그대로 남으므로, 나중에 재발급하면 이어서 쓸 수 있다.
- **기억의 원본은 회원님 GitHub 저장소**다(namu-58). 나무는 회원님이 앱 설치 때 직접 고른 저장소 한 칸에만 유효한 단명 토큰(1시간)으로 접근하며, 그 저장소의 사본을 서버에 두고 읽고 쓴 뒤 다시 밀어 올린다.

### 예전 모델(폐기됨)과의 차이

2026-07-19 최초본이 설명한 "공유 `path_secret` 1개 + `?user=` 이름표"는 **인증이 아니었다** — 공유 시크릿을 아는 사람은 남의 `user` 키만 알아내면 그 서랍을 열 수 있었다(요청자가 스스로 밝히는 값을 그대로 믿는 구조). namu-59에서 사용자별 개인 열쇠로 바뀌면서 이 결함은 사라졌고, `NAMU_HTTP_PATH_SECRET`(전원 공용 열쇠)은 설정돼 있어도 아무 효과가 없다.

## 6. 지금 안 되는 것 / 나중 계획

- 접속 주소는 사람이 브라우저로 받아 손으로 붙이는 방식이다. OAuth로 AI 클라이언트가 직접 인증을 받는 형태(동적 클라이언트 등록)는 아직 아니다 — 상세는 [`remote_mcp_design.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_design.md) §11 참고.
- `namu_sync_setup`과 쪽지 떼기·책갈피 2종은 이 경로에 노출되지 않는다(플러그인 전용).
- 웹 화면(`/auth/memory`)에서 기억을 고쳐 쓰는 것은 아직 안 된다 — 쪽지 떼기만 예외다.

**앞선 판에서 "안 된다"고 적혀 있었으나 지금은 되는 것** (2026-08-08 확인)

| 옛 서술 | 지금 |
|---|---|
| tasks는 이 경로에 노출되지 않는다 — `bowl='tasks'`로 부르면 거절한다 | **된다.** `routing_server.namu_record`의 `bowl == "tasks"` 분기가 회원 저장소 사본의 `tasks/<프로젝트>/`에 쓴다. 기록할 때 어느 프로젝트인지 함께 적어야 한다 |
| `namu_search`는 교훈 그릇만 찾는다 | **다섯 그릇 전부 찾는다.** 허용 목록을 손으로 적지 않고 코어 `cfg.BOWL_NAMES`를 그대로 본다 — 손으로 적어 두었던 탓에 코어가 그릇을 다 받은 뒤에도 이 서버만 멈춰 있던 실제 사고가 있었다 |
| 첨부 파일은 범위 밖 | **된다.** 첨부 7종이 노출되며 파일은 회원 저장소 `attach_file/`로 간다 — 상세는 [`namu_attach_files.md`](namu_attach_files.md) |

## 관련 문서

- [`install_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/install_guide.md) — Claude Code·agy용 플러그인 설치 가이드(터미널 사용자는 이쪽)
- [`remote_mcp_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_guide.md) — 경로 B(셀프호스팅) 사용 가이드
- [`remote_mcp_design.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_design.md) — 설계 원본
