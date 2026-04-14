import anthropic
import pandas as pd
from typing import List, Dict
from analyzer import get_summary_stats


def _get_client():
    """모듈 로드 시점이 아니라 호출 시 생성 (httpx/anthropic 버전 이슈·키 없을 때 import 실패 방지)"""
    return anthropic.Anthropic()

SYSTEM_PROMPT = """You are an expert technical analyst and financial researcher.
Analyze the provided stock chart image and data thoroughly.
Respond in Korean. Be direct, specific, and actionable.
Structure your response with clear sections using markdown."""

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
