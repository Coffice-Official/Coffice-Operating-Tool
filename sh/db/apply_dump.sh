#!/usr/bin/env bash
# apply_dump.sh : 단순 덤프 적용

set -euo pipefail

MYSQL_BIN=${MYSQL_BIN:-/opt/homebrew/opt/mysql-client/bin/mysql}

DST_HOST="${DST_HOST:-127.0.0.1}"
DST_PORT="${DST_PORT:-3306}"
DST_USER="${DST_USER:-root}"
DST_PASS="${DST_PASS:-1q2w3e4r}"
DST_DB="${DST_DB:-cafe}"

DATA_SQL="${DATA_SQL:-${PWD}/dump_data.sql}"
DDL_SQL="${DDL_SQL:-${PWD}/dump_ddl.sql}"

MYCNF_FILE="${PWD}/.dst.my.cnf.tmp"
DDL_PATCHED="${PWD}/dump_ddl.patched.sql"
DATA_WRAPPED="${PWD}/dump_data.wrapped.sql"

cleanup() {
  rm -f "$MYCNF_FILE" "$DDL_PATCHED" "$DATA_WRAPPED"
}
trap cleanup EXIT

# 사전 체크
command -v "$MYSQL_BIN" >/dev/null || { echo "[ERROR] mysql not found"; exit 127; }
[[ -s "$DATA_SQL" ]] || { echo "[ERROR] not found: $DATA_SQL"; exit 1; }
[[ -s "$DDL_SQL"  ]] || { echo "[ERROR] not found: $DDL_SQL"; exit 1; }

# 비밀번호 파일 생성
cat > "$MYCNF_FILE" <<EOF
[client]
password=${DST_PASS}
EOF
chmod 600 "$MYCNF_FILE"

# 연결 확인
echo "[CHECK] DST 연결"
"$MYSQL_BIN" --defaults-extra-file="$MYCNF_FILE" \
  -h "$DST_HOST" -P "$DST_PORT" -u "$DST_USER" \
  -NBe "SELECT 1" >/dev/null || { echo "[ERROR] 연결 실패"; exit 1; }

# DB 생성
echo "[STEP] DB 생성: $DST_DB"
"$MYSQL_BIN" --defaults-extra-file="$MYCNF_FILE" \
  -h "$DST_HOST" -P "$DST_PORT" -u "$DST_USER" \
  -e "CREATE DATABASE IF NOT EXISTS \`${DST_DB}\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# DEFINER 패치
echo "[STEP] DDL 패치 (DEFINER → CURRENT_USER)"
sed -E 's/DEFINER=[[:space:]]*`[^`]+`@`[^`]+`/DEFINER=CURRENT_USER/g' "$DDL_SQL" > "$DDL_PATCHED"

# DDL 적용
echo "[STEP] DDL 적용 (CREATE TABLE, TRIGGERS, ROUTINES, EVENTS)"
"$MYSQL_BIN" --defaults-extra-file="$MYCNF_FILE" \
  -h "$DST_HOST" -P "$DST_PORT" -u "$DST_USER" \
  "$DST_DB" < "$DDL_PATCHED"

# DATA 적용
echo "[STEP] DATA 적용"
if command -v pv >/dev/null 2>&1; then
  echo "[INFO] pv 사용"
  # 래핑된 SQL 파일 생성
  {
    cat <<SQL
SET FOREIGN_KEY_CHECKS=0;
SET UNIQUE_CHECKS=0;
SET AUTOCOMMIT=0;
SQL
    cat "$DATA_SQL"
    cat <<SQL
COMMIT;
SET FOREIGN_KEY_CHECKS=1;
SET UNIQUE_CHECKS=1;
SET AUTOCOMMIT=1;
SQL
  } > "$DATA_WRAPPED"

  pv "$DATA_WRAPPED" | "$MYSQL_BIN" --defaults-extra-file="$MYCNF_FILE" \
    -h "$DST_HOST" -P "$DST_PORT" -u "$DST_USER" "$DST_DB"
else
  echo "[INFO] pv 없음"
  {
    cat <<SQL
SET FOREIGN_KEY_CHECKS=0;
SET UNIQUE_CHECKS=0;
SET AUTOCOMMIT=0;
SQL
    cat "$DATA_SQL"
    cat <<SQL
COMMIT;
SET FOREIGN_KEY_CHECKS=1;
SET UNIQUE_CHECKS=1;
SET AUTOCOMMIT=1;
SQL
  } | "$MYSQL_BIN" --defaults-extra-file="$MYCNF_FILE" \
    -h "$DST_HOST" -P "$DST_PORT" -u "$DST_USER" "$DST_DB"
fi

# 검증
echo "[VERIFY] 테이블 수 확인"
TABLE_COUNT=$("$MYSQL_BIN" --defaults-extra-file="$MYCNF_FILE" \
  -h "$DST_HOST" -P "$DST_PORT" -u "$DST_USER" "$DST_DB" \
  -NBe "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${DST_DB}'")
echo "[INFO] 총 테이블 수: $TABLE_COUNT"

echo "[OK] 적용 완료"