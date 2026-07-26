#!/usr/bin/env bash
# NAMU 공용 라우팅 MCP 클라우드 컨테이너 entrypoint.
#
# 나무가 소유한 STORE git 저장소는 폐기됐다(체험판 폐기 결정). 기억의 원본은
# 이제 사용자 본인의 GitHub repo이며, 나무는 사용자가 앱 설치 시 직접 고른
# repo 한 칸에만 유효한 단명 installation token(1시간, github_app.py)으로
# 접근한다. 그래서 이 entrypoint는 더 이상 STORE 전체를 clone/pull하지 않는다
# — clone할 나무 소유 원격 자체가 없다.
#
# 왜 삭제했는지(단순 정리가 아니라 근거): 연결된 사용자의 데이터
# (NAMU_STORE_ROOT/users/<키>/)는 각 사용자 repo를 얕은 복제한 것이어야 한다.
# 이걸 나무 STORE git 작업 트리 "안"에 두면, 그 트리 전체를 나무 소유 원격으로
# push하는 순간 남의 기억이 나무 저장소로 커밋되는 사고가 된다(원격이 하나면
# 사용자별 격리를 git 차원에서 표현할 방법이 없다). 그래서 NAMU_STORE_ROOT는
# 이제 git 저장소가 아니라 **그냥 볼륨 폴더**다 — 사용자별 clone/커밋/push는
# 3차(user_repo.py 등, 이 task 범위 밖)가 사용자 repo마다 개별적으로 다룬다.
#
# 이 entrypoint가 하는 일은 이제 두 가지뿐이다:
# 1) NAMU_STORE_ROOT 볼륨 폴더가 없으면 만든다(git 작업 트리 아님, mkdir만).
# 2) git identity(user.email/user.name)가 비어 있으면 기본값을 채운다 — 사용자
#    repo에 커밋할 때(3차 소관) identity가 없으면 실패하는 실배포 갭 대비이므로
#    이 블록은 유지한다.
# 3) src/routing_server.py를 exec로 기동한다(PID 1 시그널을 그대로 전달하기
#    위해 exec 사용).
#
# 실패를 조용히 삼키지 않는다 — 각 단계는 실패 시 원인을 stderr에 남기고 즉시
# 비정상 종료(exit != 0)한다("완료" 오출력 금지).
set -euo pipefail

if [ -n "${NAMU_STORE_REMOTE:-}" ]; then
  echo "[namu-routing-entrypoint] WARNING: NAMU_STORE_REMOTE는 더 이상 사용하지 않습니다" >&2
  echo "  (나무 소유 STORE git 저장소가 폐기됐습니다 — 기억 원본은 사용자 GitHub repo입니다)." >&2
  echo "  배포 설정에서 이 환경변수를 제거하세요. 지금은 무시하고 계속 진행합니다." >&2
fi

if [ -z "${NAMU_STORE_ROOT:-}" ]; then
  echo "[namu-routing-entrypoint] ERROR: NAMU_STORE_ROOT 환경변수가 설정되지 않았습니다." >&2
  echo "  사용자별 데이터가 쌓일 볼륨 경로를 지정하세요 (예: NAMU_STORE_ROOT=/data)." >&2
  exit 1
fi

echo "[namu-routing-entrypoint] NAMU_STORE_ROOT 볼륨 준비: ${NAMU_STORE_ROOT}"
mkdir -p "${NAMU_STORE_ROOT}"

# git identity 기본값 채우기 — 이미 설정돼 있으면(이미지를 확장해 사용자가 직접
# 넣은 경우 등) 절대 덮어쓰지 않는다 — email/name을 각각 독립적으로 부재 시에만
# 채운다. 사용자 repo에 커밋할 때(3차 소관) 필요하므로 이 블록은 그대로 둔다.
if ! git config --global --get user.email > /dev/null 2>&1; then
  GIT_EMAIL="${NAMU_GIT_EMAIL:-namu@container}"
  git config --global user.email "${GIT_EMAIL}"
  echo "[namu-routing-entrypoint] git identity: user.email 미설정 — 기본값 적용(${GIT_EMAIL})"
fi
if ! git config --global --get user.name > /dev/null 2>&1; then
  GIT_NAME="${NAMU_GIT_NAME:-namu-cloud-routing}"
  git config --global user.name "${GIT_NAME}"
  echo "[namu-routing-entrypoint] git identity: user.name 미설정 — 기본값 적용(${GIT_NAME})"
fi

echo "[namu-routing-entrypoint] routing_server.py 기동"
exec python src/routing_server.py
