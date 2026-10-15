#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

IDS=(
  "ed97f4e5-ec18-45f3-a4d0-4328bce65126"
  "a950edca-9222-4675-8485-d7213d292006"
  "550f5ed3-5075-4140-a6a6-e334f7ba5645"
  "35cb9dff-5189-4b70-9a37-c1fe03cba19c"
  "d0eff05e-f554-454a-9099-95a06d3f57e0"
)

for id in "${IDS[@]}"; do
  echo "===== $id ====="
  python scripts/get_call_transcript.py --call-id "$id" --days 14
  echo
done
