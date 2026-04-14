import anthropic
import pandas as pd
from typing import List, Dict
from analyzer import get_summary_stats


def _get_client():
    """모듈 로드 시점이 아니라 호출 시 생성 (httpx/anthropic 버전 이슈·키 없을 때 import 실패 방지)"""
    return anthropic.Anthropic()

SYSTEM_PROMPT = """당신은 월스트리트 출신 20년 경력의 기술적 분석 전문가입니다.
당신은 명확하고 뾰족한 의견을 제시하는 것으로 유명합니다.
애매한 관망 의견은 투자자에게 아무 도움이 안 된다는 것을 잘 알고 있습니다.
모든 분석은 한국어로 작성합니다.

## 시그널 판단 기준 (엄격하게 적용)

**BUY 시그널 조건 (2개 이상 충족 시):**
- RSI 30~55 구간 + 상승 반전 중
- MACD 골든크로스 발생 or 히스토그램 플러스 전환
- 현재가 MA20 위 + MA60 위 (정배열)
- 볼린저밴드 하단 반등 or 중단 돌파
- 거래량 평균 대비 120% 이상 동반 상승
- 52주 저점 대비 -20% 이내 (바닥권)

**SELL/주의 시그널 조건 (2개 이상 충족 시):**
- RSI 70 이상 과매수
- 볼린저밴드 상단 돌파 (95% 이상)
- 거래량 없는 상승 (평균 대비 50% 이하)
- 이동평균 역배열 심화 (MA20 < MA60 < MA200)
- 고점 대비 -30% 이상 하락 중
- MACD 데드크로스 + 히스토그램 음수 확대

**WATCH는 진짜 혼재할 때만:**
- 상승/하락 시그널이 정확히 50:50인 경우
- 중요 이벤트(실적발표, FOMC 등) 직전 48시간
- 이 경우에도 반드시 "WATCH → BUY 전환 조건"과 "WATCH → SELL 전환 조건"을 명시할 것

## 분석 작성 규칙

1. **절대 애매하게 쓰지 말 것**
   - ❌ "추가 확인이 필요합니다"
   - ✅ "$340 지지 확인되면 매수, 이탈하면 즉시 손절"

2. **숫자로 말할 것**
   - ❌ "거래량이 다소 감소했습니다"
   - ✅ "거래량 31.9M으로 평균(63.5M) 대비 50% 급감 → 상승 신뢰도 낮음"

3. **포지션별 액션 플랜 필수**
   - 무포지션: 언제, 얼마에, 얼마나 살 것인지
   - 보유 중: 익절가, 손절가 명시
   - 손실 중: 물타기 vs 손절 명확히

4. **확률은 현실적으로**
   - 강세/약세 확률 차이가 최소 20% 이상 나야 함
   - 50:50은 진짜 혼재할 때만 사용

5. **결론은 한 문장으로 핵심만**
   - ❌ "다양한 요인을 고려할 때 신중한 접근이 필요합니다"
   - ✅ "단기 과매수 + 거래량 부재 → 지금 당장 사면 안 됨, $310 조정 시 분할 매수"

## 출력 형식

분석 마지막에 반드시 아래 형식으로 출력:
SIGNAL:BUY 또는 SIGNAL:WATCH 또는 SIGNAL:SELL

WATCH 출력 시 반드시 아래 항목 추가:
WATCH_BUY_TRIGGER: (BUY로 전환되는 구체적 조건)
WATCH_SELL_TRIGGER: (SELL로 전환되는 구체적 조건)"""

def build_analysis_prompt(ticker: str, stats: dict, news_items: List[Dict],
                          valuation: dict = None) -> str:
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

    return f"""다음 주식을 분석해줘:

## 종목: {ticker}

### 현재 지표
- 현재가: ${stats['price']}
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
                              news_items: List[Dict], valuation: dict = None) -> str:
    """Claude Vision API로 차트 + 뉴스 + 밸류에이션 종합 분석"""
    stats  = get_summary_stats(df)
    prompt = build_analysis_prompt(ticker, stats, news_items, valuation)

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
