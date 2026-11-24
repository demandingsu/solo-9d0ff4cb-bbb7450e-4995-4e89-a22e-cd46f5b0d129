#!/usr/bin/env bash
# 演示脚本:对 http://localhost:8000 走一遍完整流程(批次 -> 核对 -> 发布 -> 按工位取数)。
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"

echo "== 1. 建批:登记两个工位(新/旧终端)+ 两个项目(对称/非对称)+ 领用 =="
BATCH=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "title": "初始下发",
  "ops": [
    {"op": "upsert_item", "item_code": "ITEM-DIA-40", "name": "主轴孔直径", "nominal_um": 40000, "upper_um": 40008, "lower_um": 39992},
    {"op": "upsert_item", "item_code": "ITEM-LEN-120", "name": "轴肩长度", "nominal_um": 120000, "upper_um": 120010, "lower_um": 119995},
    {"op": "upsert_workstation", "workstation_code": "WS-01", "name": "一号加工中心", "terminal_generation": "new"},
    {"op": "upsert_workstation", "workstation_code": "WS-02", "name": "二号车床", "terminal_generation": "old"},
    {"op": "claim_item", "workstation_code": "WS-01", "item_code": "ITEM-DIA-40"},
    {"op": "claim_item", "workstation_code": "WS-01", "item_code": "ITEM-LEN-120"},
    {"op": "claim_item", "workstation_code": "WS-02", "item_code": "ITEM-DIA-40"}
  ]}')
echo "$BATCH"
BID=$(echo "$BATCH" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo; echo "== 2. 核对 =="
curl -sS -X POST "$BASE/batches/$BID/verify"

echo; echo "== 3. 发布 =="
curl -sS -X POST "$BASE/batches/$BID/publish"

echo; echo "== 4. 旧终端领用非对称项目(ITEM-LEN-120 为 +10/-5)→ 核对退回 =="
BATCH2=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "title": "旧终端领用非对称项目",
  "ops": [{"op": "claim_item", "workstation_code": "WS-02", "item_code": "ITEM-LEN-120"}]}')
BID2=$(echo "$BATCH2" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -sS -X POST "$BASE/batches/$BID2/verify"

echo; echo "== 5. 按工位取数:新终端拿上下限,旧终端拿对称公差 =="
curl -sS "$BASE/workstations/WS-01/spec"
echo
curl -sS "$BASE/workstations/WS-02/spec"
echo

echo; echo "== 6. 误发批次:把 WS-02(旧终端)升级为新终端 =="
BATCH3=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "title": "误发:WS-02 升级新终端",
  "ops": [{"op": "upsert_workstation", "workstation_code": "WS-02", "name": "二号车床", "terminal_generation": "new"}]}')
BID3=$(echo "$BATCH3" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -sS -X POST "$BASE/batches/$BID3/verify" > /dev/null
curl -sS -X POST "$BASE/batches/$BID3/publish"
echo

echo; echo "== 7. 后续批次:WS-02 领用非对称项目 ITEM-LEN-120(已是新终端,合法)=="
BATCH4=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "title": "WS-02 领用非对称项目",
  "ops": [{"op": "claim_item", "workstation_code": "WS-02", "item_code": "ITEM-LEN-120"}]}')
BID4=$(echo "$BATCH4" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -sS -X POST "$BASE/batches/$BID4/verify" > /dev/null
curl -sS -X POST "$BASE/batches/$BID4/publish"
echo

echo; echo "== 8. 后续批次:ITEM-DIA-40 收紧为 ±10µm =="
BATCH5=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "title": "ITEM-DIA-40 收紧公差",
  "ops": [{"op": "upsert_item", "item_code": "ITEM-DIA-40", "name": "主轴孔直径", "nominal_um": 40000, "upper_um": 40010, "lower_um": 39990}]}')
BID5=$(echo "$BATCH5" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -sS -X POST "$BASE/batches/$BID5/verify" > /dev/null
curl -sS -X POST "$BASE/batches/$BID5/publish"
echo

echo; echo "== 9. 只读调查:假设少了第 $BID3 批(误发的升级),后续哪些批次会失败、下发规范怎么变 =="
echo "   (批次 $BID4 回放时 WS-02 仍是旧终端 → 领用非对称项目违规,整批丢弃;批次 $BID5 继续重放)"
curl -sS "$BASE/batches/$BID3/investigate" | python3 -m json.tool
