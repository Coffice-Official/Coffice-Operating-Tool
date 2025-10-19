#!/usr/bin/env bash
# dump.sh : 모든 DB Lock-Free 덤프 (데이터/DDL 분리)

set -euo pipefail

# --- 고정 경로: 환경에 맞게 수정 ---
MYSQL_BIN=${MYSQL_BIN:-/opt/homebrew/opt/mysql-client/bin/mysql}
MYSQLDUMP_BIN=${MYSQLDUMP_BIN:-/opt/homebrew/opt/mysql-client/bin/mysqldump}

# --- 소스 설정 ---
SRC_HOST=""
SRC_PORT=3306
SRC_USER="service"
# 비밀번호는 환경변수/파라미터로 받음(파일 미사용). 예) SRC_PASS='xxxx' sh/db/dump.sh
SRC_PASS=""
SRC_DB="cafe"

DUMP_DATA="${PWD}/dump_data.sql"
DUMP_DDL="${PWD}/dump_ddl.sql"
POS_PATH="${PWD}/dump_position.txt"

# 0) 전제 확인
command -v "$MYSQL_BIN" >/dev/null || { echo "[ERROR] mysql not found: $MYSQL_BIN"; exit 127; }
command -v "$MYSQLDUMP_BIN" >/dev/null || { echo "[ERROR] mysqldump not found: $MYSQLDUMP_BIN"; exit 127; }

echo "[CHECK] SRC 연결"
MYSQL_PWD="$SRC_PASS" command "$MYSQL_BIN" \
  -h "$SRC_HOST" -P "$SRC_PORT" -u "$SRC_USER" \
  -NBe "SELECT 1" >/dev/null

# 1) 덤프 시작 좌표(스냅샷 기준)
echo "[STEP] SHOW MASTER STATUS (pre-dump)"
read -r FILE POS < <(MYSQL_PWD="$SRC_PASS" command "$MYSQL_BIN" \
  -h "$SRC_HOST" -P "$SRC_PORT" -u "$SRC_USER" \
  -NBe "SHOW MASTER STATUS" | awk '{print $1,$2}')
[[ -n "${FILE:-}" && -n "${POS:-}" ]] || { echo "[ERROR] SHOW MASTER STATUS 실패"; exit 1; }
printf "%s %s\n" "$FILE" "$POS" > "$POS_PATH"
echo "[INFO] POS 저장: $FILE $POS → $POS_PATH"

# 1.5) 대상 DB 단일 지정 확인
[[ -n "$SRC_DB" ]] || { echo "[ERROR] SRC_DB must be set (예: SRC_DB=cafe)"; exit 1; }
echo "[INFO] 대상 DB: $SRC_DB"

# 2) 데이터 덤프(FTWRL 완전 차단, 객체 정의 제외)
echo "[STEP] DATA dump → $DUMP_DATA"
MYSQL_PWD="$SRC_PASS" command "$MYSQLDUMP_BIN" \
  -h "$SRC_HOST" -P "$SRC_PORT" -u "$SRC_USER" \
  --single-transaction \
  --skip-lock-tables \
  --skip-add-locks \
  --set-gtid-purged=OFF \
  --quick \
  --no-tablespaces \
  --column-statistics=0 \
  --triggers=0 \
  --routines=0 \
  --events=0 \
  --no-create-info \
  "$SRC_DB" > "$DUMP_DATA"

# 3) 객체 정의 덤프(데이터 없이 트리거/루틴/이벤트만)
echo "[STEP] DDL dump (triggers/routines/events) → $DUMP_DDL"
MYSQL_PWD="$SRC_PASS" command "$MYSQLDUMP_BIN" \
  -h "$SRC_HOST" -P "$SRC_PORT" -u "$SRC_USER" \
  --no-data \
  --skip-lock-tables \
  --skip-add-locks \
  --set-gtid-purged=OFF \
  --no-tablespaces \
  --column-statistics=0 \
  --triggers \
  --routines \
  --events \
  "$SRC_DB" > "$DUMP_DDL"

echo "[OK] 완료: $DUMP_DATA, $DUMP_DDL, $POS_PATH"
