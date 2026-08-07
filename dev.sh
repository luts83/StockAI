#!/usr/bin/env bash
# 백엔드(FastAPI) 실행 — venv 자동 사용
# 사용: ./dev.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "ERROR: .venv 없음 → 먼저 ./setup.sh 실행" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env 없음 → .env.example 참고해 생성하거나 다른 PC에서 복사" >&2
  exit 1
fi

echo "백엔드 → http://127.0.0.1:8000"
echo "Ctrl+C 로 종료"
exec python main.py
