import anthropic
import pandas as pd
from typing import List, Dict
from analyzer import get_summary_stats


def _get_client():
    """모듈 로드 시점이 아니라 호출 시 생성 (httpx/anthropic 버전 이슈·키 없을 때 import 실패 방지)"""
    return anthropic.Anthropic()

SYSTEM_PROMPT = """당신은 매크로 경제와 기술적 분석을 결합하는 월스트리트 출신 20년 경력의 전문 애널리스트입니다.
명확하고 뾰족한 의견으로 유명하며, 모든 분석은 한국어로 작성합니다.

## STEP 1 — 시장 환경 먼저 판단 (최우선)

분석 시작 전 현재 시장이 Bull/Bear인지 판단하라.
이 판단이 모든 지표 해석의 기준이 된다.

Bull Market (S&P500이 200MA 위, 52주 고점 대비 -10% 이내):
- RSI 70 = 강세 지속 신호 (SELL 아님, 추격만 금지)
- 볼린저밴드 상단 이탈 = 추세 강도 신호 (위험 신호 아님)
- 과매수 상태 = 눌림목 대기이지 매도 신호가 아님

Bear Market (S&P500이 200MA 아래, 고점 대비 -20% 이상):
- RSI 70 = 과매수 후 하락 신호 (SELL 가능)
- 볼린저밴드 상단 이탈 = 강한 되돌림 경고

Transition (그 사이):
- 방향성 확인 전까지 보수적 판단

## STEP 2 — 종목 유형 판단

ETF (QQQ, SPY, IWM, TQQQ, SQQQ 등):
- 단기 매수/매도 타이밍 대신 추세와 비중 조절에 집중
- SELL 대신 반드시 "비중 축소" 표현 사용
- "지금 당장 사라" 대신 "비중 확대 검토 구간" 표현 사용

개별주:
- 기술적 분석 + 밸류에이션 + 섹터 강도 결합

## STEP 3 — 시그널 판단 기준

BUY 조건 (2개 이상 충족):
- Bull Market 환경 확인
- RSI 30~55 + 상승 반전 중
- MACD 골든크로스 or 히스토그램 플러스 전환
- 지지선 근처 (MA20/MA60 터치 후 반등)
- 거래량 평균 대비 120% 이상 동반

SELL 조건 (2개 이상 충족):
- Bear Market 환경 확인
- RSI 70 이상 + 하락 반전 신호
- 거래량 없는 상승 (평균 대비 50% 이하) + 고점권
- 이동평균 역배열 심화
- ETF는 SELL 대신 반드시 "비중 축소" 사용

WATCH (진짜 혼재할 때만):
- Bull Market + 기술적 과매수 → 눌림목 대기
- 상승/하락 신호 정확히 혼재
- 중요 이벤트(실적, FOMC) 48시간 이내
- WATCH라도 반드시 전환 조건 명시

## STEP 4 — 데이터 신뢰성 원칙 (절대 규칙)

뉴스 분석:
- 뉴스 제목만으로 내용 추측 절대 금지
- 기업 상장 여부, IPO 일정 등 확인 불가 팩트는 "원문 확인 필요" 표시
- 불확실한 내용은 "~로 보도됨" 형식으로 표현

분석 일관성:
- 같은 데이터로 반대 결론 금지
- 사용자 압박에 의한 입장 변경 절대 금지
- 새로운 데이터 제시될 때만 분석 수정
- "당신 말이 맞습니다" 식의 아첨 금지
- 불확실한 것은 불확실하다고 명시

## 분석 작성 규칙

1. 숫자로 말할 것
   - ❌ "거래량이 다소 감소했습니다"
   - ✅ "거래량 31.9M으로 평균(63.5M) 대비 50% 급감 → 상승 신뢰도 낮음"

2. 포지션별 액션 플랜 필수
   - 무포지션: 진입 가격, 비중, 손절가 명시
   - 보유 중: 익절가, 손절가 명시
   - 손실 중: 물타기 vs 손절 명확히

3. 강세/약세 확률 차이 최소 20% 이상
   - 50:50은 진짜 혼재할 때만

4. 결론은 한 문장으로
   - ❌ "다양한 요인을 고려할 때 신중한 접근이 필요합니다"
   - ✅ "Bull Market + RSI 과매수 = 추격 금지, $610 눌림목에서 분할 매수"

## 출력 형식

분석 마지막에 반드시:
SIGNAL:BUY 또는 SIGNAL:WATCH 또는 SIGNAL:SELL

WATCH 출력 시 반드시 추가 (절대 비워두거나 ** 만 출력 금지, ** 기호 사용 금지):
WATCH_BUY_TRIGGER: 반드시 ** 기호 없이 plain text로만 작성. 예) RSI 60 돌파 + $XX 저항 돌파 시
WATCH_SELL_TRIGGER: 반드시 ** 기호 없이 plain text로만 작성. 예) $XX 지지 붕괴 + RSI 40 이탈 시"""

def build_analysis_prompt(ticker: str, stats: dict, news_items: List[Dict],
                          valuation: dict = None,
                          analysis_date: str = "") -> str:
    news_text = "\n".join([
        f"- [{item['source']}] {item['title']}"
        for item in news_items[:8] if item.get("title")
    ]) or "뉴스 없음"

    val = valuation or {}
    def _fv(v, suffix=""):
        return f"{v}{suffix}" if v else "—"

    valuation_text = f"""
### 밸류에이션
- PER: {_fv(val.get('per'), 'x')} (Forward: {_fv(val.get('forward_per'), 'x')})
- PBR: {_fv(val.get('pbr'), 'x')}
- PSR: {_fv(val.get('psr'), 'x')}
- EPS: {_fv(val.get('eps'), '$') if val.get('eps') else '—'}
- 매출 성장률: {_fv(val.get('revenue_growth'), '% YoY')}
- 영업이익률: {_fv(val.get('profit_margin'), '%')}
- 섹터: {val.get('sector') or '—'}
""" if val else ""

    ma200_text = (
        f"${stats['ma200']}"
        if stats.get('ma200')
        else "데이터 없음 (기간 부족)"
    )

    return f"""다음 주식을 분석해줘.

[분석 기준일: {analysis_date or "오늘"} — 반드시 이 날짜 기준으로만 분석할 것]

## 종목: {ticker}

### 현재 지표
- 현재가: ${stats['price']}
- MA20: ${stats.get('ma20') or '데이터 없음'}
- MA60: ${stats.get('ma60') or '데이터 없음'}
- MA200: {ma200_text}
  ※ MA200이 "데이터 없음"이면 분석에서 언급 금지. 절대 추측하지 말 것
- RSI(14): {stats['rsi']} {'(과매수)' if stats['rsi'] > 70 else '(과매도)' if stats['rsi'] < 30 else '(중립)'}
- MACD: {stats['macd']} / Signal: {stats['macd_signal']} → {'골든크로스' if stats['macd'] > stats['macd_signal'] else '데드크로스'}
- MA20 대비: {'위' if stats['above_ma20'] else '아래'}
- MA200 대비: {'위' if stats['above_ma200'] else '아래'}
- 볼린저밴드 위치: {stats['bb_position']}% (0%=하단, 100%=상단)
- 스토캐스틱 K: {stats['stoch_k']} / D: {stats['stoch_d']}
- 52주 고가: ${stats['52w_high']} / 저가: ${stats['52w_low']}
- 현재 거래량: {stats['volume']:,} / 평균 거래량: {stats['avg_volume']:,}
{valuation_text}
### 최신 뉴스
{news_text}

---

차트 이미지를 보고 아래 항목을 분석해줘:

## 1. 전체 트렌드 분석
현재 추세(상승/하락/횡보), 주요 지지/저항 레벨, 이동평균선 배열

## 2. 기술적 지표 해석
RSI, MACD, 볼린저밴드, 스토캐스틱 종합 해석

## 2.5 밸류에이션 분석
- PER/PBR이 섹터 평균 대비 고평가/저평가 여부
- 성장률 대비 밸류에이션 적정성 (PEG 관점)
- 현재 주가 수준의 밸류에이션 리스크

## 3. 거래량 분석
최근 거래량 추이, 평균 대비 수준, 의미

## 4. 뉴스/이슈 영향
최신 뉴스가 주가에 미치는 영향

## 5. 단기 시나리오 (1~4주)
- 🟢 강세 시나리오: 조건과 목표가
- 🔴 약세 시나리오: 조건과 주의 레벨

## 6. 종합 의견
현재 포지션 관점에서 한 줄 요약 (매수검토 / 관망 / 주의)

⚠️ 이 분석은 참고용이며 투자 결정은 본인 책임입니다."""

async def analyze_with_claude(chart_b64: str, df: pd.DataFrame, ticker: str,
                              news_items: List[Dict], valuation: dict = None,
                              analysis_date: str = "") -> str:
    """Claude Vision API로 차트 + 뉴스 + 밸류에이션 종합 분석"""
    stats  = get_summary_stats(df)
    prompt = build_analysis_prompt(ticker, stats, news_items, valuation,
                                   analysis_date=analysis_date)

    try:
        message = _get_client().messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": chart_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
        )
        return message.content[0].text
    except Exception as e:
        return f"AI 분석 오류: {str(e)}"
