# namu-cloud-routing

NAMU 공용 라우팅 MCP 서비스. 요청마다 사용자 키를 읽어 **포터블 메모리 코어**(namu-agent)를
그 사용자 전용 데이터 디렉토리로 라우팅한다. 개인용 NAMU(단일 데이터 루트, stdio)와는 완전히
분리된 별도 서비스다.

## 아키텍처 (namu-50 결정)

- **코어는 복제하지 않는다.** 메모리 저장 로직(recall/record/search·스키마)은 namu-agent에
  단일 원본으로 남고, 이 repo는 그것을 **git submodule**(`vendor/namu-agent`, 태그 핀)로
  재사용한다. 코어에 라우팅 로직을 넣지 않고, 코어가 연 "데이터 루트 이음새"
  (`config.data_paths_for(root)`)만 소비한다.
- **저장소 모델**: 한 STORE repo(`namu-cloud-memory`) 안 `users/<사용자키>/` 하위디렉토리.
  각 디렉토리는 개인용 `~/.namu`와 같은 구조(`memory/learnings.yaml`·`memory/profile.yaml`·
  `db/namu.db`).
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

## 범위 (현 단계)

임시 사용자 1명 디렉토리 + 요청별 라우팅 동작까지. git 발급·사용자 write의 STORE
push-back·동기화·동시성은 후속.
