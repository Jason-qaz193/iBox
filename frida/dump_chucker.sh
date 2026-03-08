#!/usr/bin/env bash
# Pull Chucker HTTP log database from iBox and dump request/response bodies.
# Chucker stores plaintext HTTP traffic (before encryption) in a local SQLite DB.
# Since ChuckerInterceptor is AFTER HttpRequestEncrypt in iBox's interceptor chain,
# the stored body is the ENCRYPTED body — exactly what we need to see the HTTP format.
#
# Run after triggering a login in iBox.
#
# Requirements: adb, sqlite3 (brew install sqlite or apt install sqlite3)

set -e

PKG="com.box.art"
REMOTE_DB_PATH="/data/data/${PKG}/databases"
LOCAL_DIR="./chucker_dump"

mkdir -p "$LOCAL_DIR"

echo "[1] Finding Chucker database on device..."
DB_NAME=$(adb shell "su -c 'ls ${REMOTE_DB_PATH}'" 2>/dev/null | grep -i "chuck" | tr -d '\r' | head -1)

if [ -z "$DB_NAME" ]; then
    echo "    No chuck*.db found. Trying common names..."
    for name in chuck_db chucker.db http_transactions.db; do
        CHECK=$(adb shell "su -c 'ls ${REMOTE_DB_PATH}/${name} 2>/dev/null'" | tr -d '\r')
        if [ -n "$CHECK" ]; then
            DB_NAME="$name"
            break
        fi
    done
fi

if [ -z "$DB_NAME" ]; then
    echo "[!] Chucker DB not found. List all databases:"
    adb shell "su -c 'ls -la ${REMOTE_DB_PATH}'"
    exit 1
fi

echo "[2] Found: ${DB_NAME}. Pulling..."
adb shell "su -c 'cp ${REMOTE_DB_PATH}/${DB_NAME} /sdcard/Download/chuck_dump.db'"
adb pull "/sdcard/Download/chuck_dump.db" "${LOCAL_DIR}/chuck_dump.db"
echo "    Saved to ${LOCAL_DIR}/chuck_dump.db"

echo ""
echo "[3] Dumping recent HTTP transactions..."
sqlite3 "${LOCAL_DIR}/chuck_dump.db" <<'SQL'
.headers on
.mode column
.width 5 6 50 80 80

SELECT
    id,
    method,
    path,
    substr(request_body, 1, 200) AS req_body,
    substr(response_body, 1, 200) AS resp_body
FROM transactions
ORDER BY id DESC
LIMIT 20;
SQL

echo ""
echo "[4] Detailed dump (login-related)..."
sqlite3 "${LOCAL_DIR}/chuck_dump.db" <<'SQL'
.headers on
.mode line

SELECT
    id,
    method,
    scheme || '://' || host || path AS url,
    request_headers,
    request_body,
    response_code,
    response_headers,
    response_body
FROM transactions
WHERE path LIKE '%login%' OR path LIKE '%sendSms%'
ORDER BY id DESC
LIMIT 5;
SQL

echo ""
echo "[done] Full DB at: ${LOCAL_DIR}/chuck_dump.db"
echo "       Open with: sqlite3 ${LOCAL_DIR}/chuck_dump.db"
