# namu-cloud-routing

NAMU 공용 라우팅 MCP 서비스. 요청마다 사용자 키를 읽어 **포터블 메모리 코어**(namu-agent)를
그 사용자 전용 데이터 디렉토리로 라우팅한다. 개인용 NAMU(단일 데이터 루트, stdio)와는 완전히
분리된 별도 서비스다.

## 아키텍처 (namu-50 결정)

- **코어는 복제하지 않는다.** 메모리 저장 로직(recall/record/search·스키마)은 namu-agent에
  단일 원본으로 남고, 이 repo는 그것을 **git submodule**(`vendor/namu-agent`, 태그 핀)로
  재사용한다. 코어에 라우팅 로직을 넣지 않고, 코어가 연 "데이터 루트 이음새"
  (`config.data_paths_for(root)`)만 가져다 쓴다.
- **저장소 모델(namu-58로 변경)**: 기억의 원본은 단일 공용 STORE repo가 아니라 **사용자 본인이
  연결한 GitHub 저장소**다. `STORE_ROOT/users/<사용자키>/`는 그 저장소의 순수 캐시(`user_repo.py`가
  단명 토큰으로 clone/fetch/push)일 뿐이며, 지워도 원본은 사용자 GitHub에 남는다. 각 캐시 디렉토리는
  개인용 `~/.namu`와 같은 구조를 쓴다(`memory/learnings.yaml`·`memory/profile.yaml`·`db/namu.db`).
  (예전에는 `namu-cloud-memory` 하나에 사용자별 하위디렉토리를 두는 단일 STORE 모델이었으나,
  namu-58에서 사용자별 개인 저장소 연결로 대체되며 폐기됐다.)
- **라우팅**: 요청 `.../mcp/<사용자키>` → 데이터 루트를 `<STORE clone>/users/<사용자키>`로
  갈아끼워 코어 호출.

## 구조

```
vendor/namu-agent/   ← git submodule (namu-agent @ 태그 핀)
  namu-plugin/{config,db,profile}.py   ← 코어(이 repo가 재사용)
src/routing_server.py                  ← 라우팅 MCP 서버
tests/
```

## 서브모듈 초기화

clone 후:

```
git submodule update --init --recursive
```

## 문서

- [`docs/namu_cloud_guide.md`](docs/namu_cloud_guide.md) — 공용 클라우드 MCP(경로 A) 사용 가이드
- [`docs/namu_attach_files.md`](docs/namu_attach_files.md) — 첨부 파일 주고받기(완료 보고서) — 도구 일곱 개·티켓 주소·서버 사본 격리·크기 상한 실측

## 범위 (현 단계)

지금까지 된 것: 임시 사용자 1명 디렉토리 생성 + 요청별 라우팅 동작. git 키 발급, 사용자가
쓴 데이터를 STORE로 되밀어 넣기(push-back), 동기화, 동시성 처리는 다음 단계로 남겨 뒀다.
