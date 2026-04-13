#!/bin/bash
# 백엔드(FastAPI auto-reload) + 프론트(livereload) 동시 실행
# 사용: ./dev.sh  또는  bash dev.sh

cleanup() {
  echo ""
  echo "서버 종료 중..."
  kill $BACKEND_PID $FRONTEND_PID 2>/dev/null
  exit 0
}
trap cleanup SIGINT SIGTERM

echo "🚀 백엔드  → http://localhost:8000"
echo "🌐 프론트  → http://127.0.0.1:5500/index.html"
echo "Ctrl+C 로 종료"
echo "-------------------------------------------"

python main.py &
BACKEND_PID=$!

python serve.py &
FRONTEND_PID=$!

wait
