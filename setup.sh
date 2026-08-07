#!/usr/bin/env bash
# StockAI — 어느 머신에서든 동일하게 로컬 환경 구성
# 사용: ./setup.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "==> StockAI setup"
echo "    cwd: $ROOT"

# ── Python 선택 (runtime.txt / .python-version 기준 3.12 우선) ──
pick_python() {
  for candidate in python3.12 python3.12.8 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      ver="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      major="${ver%%.*}"
      minor="${ver#*.}"
      if [[ "$major" -eq 3 && "$minor" -ge 10 ]]; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  echo "ERROR: Python 3.10+ 필요. 예: brew install python@3.12" >&2
  exit 1
}

PY="$(pick_python)"
PY_VER="$("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
echo "==> Python: $PY ($PY_VER)"

# ── venv ──
if [[ ! -d .venv ]]; then
  echo "==> Creating .venv"
  "$PY" -m venv .venv
else
  echo "==> .venv already exists"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Upgrading pip"
python -m pip install --upgrade pip >/dev/null

echo "==> Installing requirements.txt"
pip install -r requirements.txt

echo "==> Installing Playwright Chromium (카드 PNG용)"
python -m playwright install chromium || {
  echo "WARNING: playwright browser install failed — 카드 생성만 영향" >&2
}

# ── .env ──
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "==> Created .env from .env.example"
  echo "    ⚠️  다른 PC의 .env 값을 복사하거나 키를 채워 주세요."
else
  echo "==> .env already present (kept as-is)"
fi

# 누락 키 힌트
missing=()
while IFS= read -r key; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  name="${key%%=*}"
  val="$(grep -E "^${name}=" .env | head -1 | cut -d= -f2- || true)"
  if [[ -z "$val" || "$val" == your_* || "$val" == *"<"* ]]; then
    missing+=("$name")
  fi
done < <(grep -E '^[A-Z_]+=' .env.example || true)

if ((${#missing[@]})); then
  echo ""
  echo "==> .env 에서 확인이 필요한 키:"
  for m in "${missing[@]}"; do
    echo "    - $m"
  done
fi

echo ""
echo "✅ Setup complete"
echo ""
echo "다음:"
echo "  source .venv/bin/activate"
echo "  python main.py          # http://127.0.0.1:8000"
echo "  # 또는  ./dev.sh        # 백엔드 + livereload (serve.py 필요)"
echo ""
echo "다른 PC로 옮길 때:"
echo "  1) git push / pull 로 코드 동기화"
echo "  2) 이 머신에서 ./setup.sh 한 번"
echo "  3) .env 는 git에 없음 → 기존 PC에서 복사하거나 비밀저장소에서 복원"
