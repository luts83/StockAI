# StockAI — AI Stock Chart Analyzer

> 티커 하나로 차트 + 기술적 지표 + 실시간 뉴스를 AI가 종합 분석합니다.  
> An AI-powered tool that analyzes charts, technical indicators, and real-time news from a single stock ticker.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green) ![Claude](https://img.shields.io/badge/Claude-Anthropic-orange) ![License](https://img.shields.io/badge/License-MIT-yellow)

**Repository:** [github.com/luts83/StockAI](https://github.com/luts83/StockAI)

---

## Features

| | |
|--|--|
| Auto chart | 캔들 + MA20/60/200 + 볼린저 + RSI + MACD + 거래량 |
| AI analysis | Claude Vision으로 차트·지표·뉴스 종합 리포트 (한국어) |
| Live news | Yahoo Finance + Google News RSS, 제목 한글 번역 |
| News summary | 뉴스 항목 탭 시 스트리밍 요약 |
| UI | 반응형 단일 `index.html` |

---

## Tech stack

FastAPI · yfinance · pandas-ta · matplotlib · feedparser · Anthropic API · vanilla HTML/JS

---

## Quick start (local)

```bash
git clone https://github.com/luts83/StockAI.git
cd StockAI
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# .env 에 ANTHROPIC_API_KEY 설정
python main.py
```

브라우저에서 **http://127.0.0.1:8000/** 를 열면 UI가 표시됩니다 (API와 동일 출처).

---

## Project layout

```
StockAI/
├── main.py          # FastAPI + / (index.html)
├── analyzer.py      # 데이터·지표
├── chart.py         # 차트 이미지
├── news.py          # 뉴스·제목 번역
├── ai.py            # Claude Vision 분석
├── index.html       # 프론트엔드
├── requirements.txt
├── .env.example
├── render.yaml      # Render 배포 예시
└── Procfile         # Heroku/Railway 등
```

---

## Deploy (Render 권장)

1. [GitHub 저장소](https://github.com/luts83/StockAI)에 코드 푸시 (아래 Git 명령 참고).
2. [Render](https://render.com) → **New +** → **Web Service** → 해당 repo 연결.
3. 설정 예시:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. **Environment** → `ANTHROPIC_API_KEY` 추가 (Secret).
5. 배포 후 제공 URL(예: `https://stockai.onrender.com`)로 접속.

`render.yaml`이 있으면 Blueprint로 한 번에 생성할 수 있습니다.

### Railway / Fly.io

- **Start:** `uvicorn main:app --host 0.0.0.0 --port $PORT` (또는 플랫폼 기본 포트 변수)
- **Env:** `ANTHROPIC_API_KEY` 필수

---

## API

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | 웹 UI (`index.html`) |
| POST | `/analyze` | 티커 분석 JSON |
| POST | `/news/summary` | 뉴스 스트리밍 요약 (text/plain) |
| GET | `/health` | 헬스체크 |

`POST /analyze` body 예: `{"ticker":"AAPL","period":"6mo","interval":"1d"}`

---

## Environment variables

| Variable | 설명 |
|----------|------|
| `ANTHROPIC_API_KEY` | [Anthropic Console](https://console.anthropic.com) 에서 발급 |
| `PORT` | 배포 플랫폼이 자동 설정 (로컬 기본 8000) |

`.env`는 절대 커밋하지 마세요.

---

## Disclaimer

이 도구는 **참고용**입니다. 투자 책임은 본인에게 있습니다. yfinance는 비공식 API이며 상업적 이용에 제한이 있을 수 있습니다.

---

## License

MIT
