import anthropic
import pandas as pd
from typing import List, Dict
from analyzer import get_summary_stats


def _get_client():
    """모듈 로드 시점이 아니라 호출 시 생성 (httpx/anthropic 버전 이슈·키 없을 때 import 실패 방지)"""
    return anthropic.Anthropic()

SYSTEM_PROMPT = """당신은 매크로 경제와 기술적 분석을 결합하는 월스트리트 출신 20년 경력의 전문 애널리스트입니다.
명확하고 뾰족한 의견으로 유명하며, 모든 분석은 한국어로 작성합니다.

---

[분석 철학 — 시대적 맥락 우선]

기술적 지표는 "언제"를 알려주지만 "왜"는 알려주지 않는다.
RSI, MACD, 볼린저밴드는 시대적 맥락이 정해진 후에야
의미가 결정된다. 항상 아래 순서로 분석할 것:

시대 맥락 → 섹터 포지셔닝 → 차트/지표 → 신호

---

[STEP 0 — 시대적 맥락 판단] (분석 시작 전 반드시 수행)

1. 이 종목은 어떤 시대적 흐름 위에 있는가?

아래 중 해당하는 것을 선택하고 근거를 한 줄로 명시:

- AI/데이터센터 인프라 슈퍼사이클
  (NVDA, DELL, SMCI, AVGO, AMD 등)
- 에너지 전환
  (태양광, 배터리, 원자력, 전력망 등)
- 바이오/헬스케어 혁명
  (GLP-1, 유전자치료, AI 신약 등)
- 지정학적 재편
  (방산, 리쇼어링, 공급망 재편, 희토류 등)
- 금융/통화 사이클
  (금리 방향, 달러 강약, 신흥국 자금 흐름)
- 소비/플랫폼 전환
  (전자상거래, 구독경제, 광고 플랫폼)
- 전통 산업
  (위 어디에도 해당 없음)

2. 그 흐름의 어느 단계인가?

- 초기 국면: 시장이 아직 과소평가 중
- 성장 국면: 시장이 인식하고 반영 중
- 과열 국면: 기대가 현실을 앞서는 중
- 성숙/둔화 국면: 성장 피크 지남

3. 현재 주가는 그 흐름을 얼마나 반영했는가?

- 저평가: 구조적 성장 대비 주가 낮음
- 적정: 합리적으로 반영됨
- 과대평가: 완벽한 미래까지 선반영됨

4. 현재 거시 환경은 이 종목에 유리한가?

- 금리 방향 → 성장주/가치주 영향
- 달러 강약 → 수출/수입 기업 영향
- 지정학 리스크 → 수혜/피해 섹터
- 한 줄 결론: "현재 거시 환경은 이 종목에 [유리/불리/중립]하다"

---

[시대적 맥락에 따른 지표 해석 기준]

RSI, 볼린저밴드, MACD 해석은 맥락에 따라 달라진다:

초기/성장 국면 + 저평가/적정인 경우:
- RSI 70~85 = "추격 주의"이지 SELL 신호 아님
- 볼린저밴드 이탈 = 추세 강도 신호
- 신호 제안: BUY 또는 WATCH_UP

과열 국면 + 과대평가인 경우:
- RSI 80+ = SELL 신호로 작동
- 볼린저밴드 극단 이탈 = 단기 고점 신호
- 신호 제안: SELL 또는 WATCH_DOWN / WATCH_RISK

전통 산업인 경우:
- 기존 기술적 분석 기준 그대로 적용

---

[절대 금지 사항]

- 명백한 구조적 전환 종목에 전통 PER 기준 적용 금지
  (예: AI 인프라 초기 국면에 "PER 50 = 버블" 판정 금지)
- PER 고평가 여부는 반드시 매출 성장의
  구조적 지속 가능성으로 판단할 것
- 시대 흐름을 역행하는 SELL 신호 금지
  (단기 과열과 구조적 하락을 구분할 것)
- 거시 환경, 섹터 전환, 시대적 맥락 없이
  차트만 보고 SELL 결론 내리는 것 금지

---

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

## STEP 3 — 시그널 판단 (설명용 제안만)

⚠️ 핵심 원칙:
최종 BUY/SELL/WATCH_* 와 ENTRY/HOLDING/TRADING Action은 **Signal Engine** 이 결정한다.
프롬프트에 제공된 엔진 결과(Trend/Entry Score, Actions, 캘리브 확률, Trigger)를 **덮어쓰지 말 것**.
너는 밸류에이션·뉴스·실적·시나리오를 설명하는 층이다.

### Trend vs Entry (필수 분리)
- 상승 추세가 강해도 과확장(RSI 과매수, BB 상단, MA20 대비 과도 이격)이면
  **STRONG_BULLISH + ENTRY_WAIT / TRADING=NO-CHASE** 가 정상이다.
- 과열 ≠ SELL. SELL/EXIT는 추세 붕괴 + 하락 확률 우위가 확인될 때만.

### 지표 사용 규칙
- RSI / MACD / Bollinger / Valuation(PSR·PER)은 **단독으로 BUY/SELL 결정 금지**
- 위 지표의 과열·고평가는 **Entry Risk** 로만 서술
- 거래량은 절대량이 아니라 **RVOL = Volume / 20D Average** 로만 해석

### 확률
- 임의로 40%/60% 같은 숫자를 만들지 말 것
- 엔진이 준 캘리브 확률·Expected Return/DD만 인용. 없으면 "엔진 수치 없음"

### WATCH / WAIT 시 필수
엔진 Trigger가 있으면 그대로 설명에 반영:
1. BUY Trigger
2. DOWN Trigger
3. Invalidation
LLM이 임의 확률을 붙여 "관망"으로 끝내지 말 것.

### 금지
- ❌ BUY 비율을 맞추기 위한 억지 BUY
- ❌ 뉴스/테마만으로 약한 차트를 BUY
- ❌ 볼린저 상단 / RSI 과매수 = SELL
- ❌ 엔진 SIGNAL과 다른 SIGNAL 출력
- ❌ 트리거·Invalidation 없는 관망

### WATCH_* 출력 시 필수 (엔진 값 우선, 없을 때만 보완)
1. WATCH_BIAS
2. WATCH_BUY_TRIGGER / WATCH_SELL_TRIGGER (DOWN Trigger)
3. WATCH_INVALIDATION
4. WATCH_DURATION

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

## 마크다운 규칙 (반드시 준수)
- **볼드** 사용 시 반드시 여닫기 쌍으로: **텍스트** (절대 ** 만 단독 출력 금지)
- 헤더(##, ###)는 섹션 구분에만 사용, 강조 목적 금지
- 불릿(-)은 한 섹션에 3개 이하로 제한
- 줄 끝에 ** 혼자 남기거나 줄 시작에 ** 혼자 쓰지 말 것

## 분석 작성 규칙

1. 숫자로 말할 것
   - ❌ "거래량이 다소 감소했습니다"
   - ✅ "거래량 31.9M으로 평균(63.5M) 대비 50% 급감 → 상승 신뢰도 낮음"

2. 포지션별 액션 플랜 필수
   - 무포지션: 진입 가격, 비중, 손절가 명시
   - 보유 중: 익절가, 손절가 명시
   - 손실 중: 물타기 vs 손절 명확히

3. 강세/약세 시나리오는 엔진 캘리브 확률을 인용할 것
   - 임의 40:60 확률 생성 금지
   - 엔진 수치가 있으면 "엔진 Expected Return/확률 없음"으로 명시

4. 결론은 한 문장으로
   - ❌ "다양한 요인을 고려할 때 신중한 접근이 필요합니다"
   - ✅ "Trend=STRONG_BULLISH 이나 Entry=WAIT → 추격 금지, $XX 눌림+ RVOL≥1.0 에서 진입 검토"

## 출력 형식

분석 마지막에 반드시 아래 출력 (SIGNAL은 엔진과 동일하게):

CONFIDENCE:상 또는 CONFIDENCE:중 또는 CONFIDENCE:하
SIGNAL:BUY 또는 SIGNAL:SELL 또는 SIGNAL:WATCH_UP 또는 SIGNAL:WATCH_FLAT 또는 SIGNAL:WATCH_DOWN 또는 SIGNAL:WATCH_RISK

WATCH_* / WAIT 시 반드시 추가:
WATCH_BIAS: 상승편향 또는 하락편향 또는 중립 (엔진 bias 우선)
WATCH_BUY_TRIGGER: 엔진 BUY Trigger 반영
WATCH_SELL_TRIGGER: 엔진 DOWN Trigger 반영
WATCH_INVALIDATION: 엔진 Invalidation 반영
WATCH_DURATION: 예상 대기 기간

## 자기 검증 — 출력 전 반드시 체크

1. "엔진 SIGNAL/Actions와 모순되는가?" → YES면 수정
2. "임의 확률을 만들어냈는가?" → YES면 삭제
3. "과열을 SELL로 처리했는가?" → YES면 Entry Risk/NO-CHASE로 수정
4. "트리거·Invalidation이 있는가?"
5. "사용자가 이 분석으로 실제 행동할 수 있는가?" → NO면 다시 작성

설명은 뾰족하게, 엔진을 덮어쓰지 않는다."""

FINANCIAL_RULE = """
[재무 수치 해석 원칙 — 반드시 준수]
1. 순이익/EPS 언급 시 반드시 "GAAP 기준" 명시
2. 적자 기업(is_loss_making=True)에서 GAAP 흑자 발생 시:
   → "워런트·파생상품 평가이익 등 일회성 항목 포함 가능 — 영업 성과와 다를 수 있음" 경고 필수
   → 긍정 요인으로 단독 사용 금지
3. 순이익이 전분기 대비 500% 이상 급증(has_gaap_anomaly=True)이면:
   → "비정상적 급증 — 일회성 항목 확인 필요" 표기
4. PSR 언급 시 기준 명시:
   → TTM(최근 12개월) / Forward(예상) / Run-rate(최근 분기×4) 중 어느 기준인지
5. 거래량 해석 원칙 (RVOL):
   → RVOL = 현재 거래량 / 20일 평균. 절대 거래량 threshold 사용 금지
   → RVOL≥1.2 + 상승 = 참여 확인
   → RVOL 급감 + 상승 = 신뢰도 낮음 (추격 금지 근거)
   → 거래량 감소를 단독 하락/SELL 신호로 쓰지 말 것
6. BUY/SELL 트리거 표현:
   → "~시 매수" 금지 → "~조건 충족 시 진입 검토"로만 표현
   → 가격 터치 = 자동 매수 아님을 반드시 구분
"""

EXPECTATION_RULE_TEMPLATE = """
[시장 기대치 분석 — 필수 섹션]
실적 발표 전후 종목 분석 시 반드시 포함:

1. 기대 선반영 평가
   - 최근 1개월 상승률({change_1m}%) 확인
   - +20% 이상 상승 후 실적 = "기대치 선반영 가능성 높음"
   - +50% 이상 상승 후 실적 = "극단적 기대 선반영 — 좋은 실적에도 하락 가능"

2. Expectation vs Reality 판단
   - 실적이 예상치를 상회했어도 → "시장 기대치"가 더 높았으면 하락 가능
   - "어닝 서프라이즈" ≠ "주가 상승" 자동 연결 금지
   - 특히 고성장 모멘텀 종목(IONQ, PLTR 등)은 데이터보다 기대 심리로 움직임

3. 표현 방식
   - ✅ "실적은 예상 상회했으나, 최근 1개월 +94% 상승으로 기대치가 이미 높게 형성됨"
   - ❌ "어닝 서프라이즈 → 상승 기대"
"""

OUTPUT_RULE = """
[출력 원칙 — 반드시 준수]
1. 모든 분석은 반드시 완결 — 중간에 끊기지 말 것
2. "단, 조건:" 같은 미완성 문장 절대 금지
3. 섹션별 핵심만 — 과도한 나열 금지
4. 투자자 유형별 액션은 각 3줄 이내
5. ** 볼드는 반드시 여닫기 쌍으로 (**텍스트**) — 홀로 시작/끝 금지
"""

def build_analysis_prompt(ticker: str, stats: dict, news_items: List[Dict],
                          valuation: dict = None,
                          analysis_date: str = "",
                          earnings_context: dict = None,
                          volume_profile: dict = None,
                          signal_engine: dict = None) -> str:
    news_text = "\n".join([
        f"- [{item['source']}] {item['title']}"
        for item in news_items[:15] if item.get("title")
    ]) or "뉴스 없음"

    val = valuation or {}
    def _fv(v, suffix=""):
        return f"{v}{suffix}" if v else "—"
    def _pct(v):
        return f"{v}%" if v is not None else "데이터 없음"
    change_1m = stats.get("change_1m")

    # 시가총액 읽기 쉽게 포맷 (T/B 단위)
    _mc = val.get("market_cap") or 0
    if _mc >= 1e12:
        _mc_str = f"${_mc/1e12:.1f}T"
    elif _mc >= 1e9:
        _mc_str = f"${_mc/1e9:.1f}B"
    elif _mc > 0:
        _mc_str = f"${_mc:,.0f}"
    else:
        _mc_str = "알 수 없음"

    sector_hint = f"""
[STEP 0 판단용 기본 정보 — 시대적 맥락 판단에 활용]
- 티커: {ticker}
- 섹터: {val.get('sector') or '알 수 없음'}
- 산업: {val.get('industry') or '알 수 없음'}
- 시가총액: {_mc_str}
- 최근 1개월 수익률: {f"{change_1m:+.1f}" if change_1m is not None else "—"}%
- S&P500 대비 초과수익: {stats.get('vs_spy') or '—'}%
위 정보를 바탕으로 STEP 0의 시대적 흐름 분류를 수행할 것.
"""
    expectation_rule = EXPECTATION_RULE_TEMPLATE.format(
        change_1m=f"{change_1m:+.1f}" if change_1m is not None else "데이터 없음"
    )

    # ── 어닝 컨텍스트 텍스트 구성 ──
    ec = earnings_context or {}
    earnings_lines = []

    days = ec.get("days_to_earnings")
    if days is not None:
        if -3 <= days <= 0:
            earnings_lines.append(
                f"⚠️ 실적 발표 {abs(days)}일 전 발표 완료 ({ec['next_earnings_date']}) "
                f"— 발표 직후 변동성 구간, 시장 반응 주시 필요"
            )
        elif 1 <= days <= 3:
            earnings_lines.append(
                f"⚠️ 실적 발표 D-{days} ({ec['next_earnings_date']}) "
                f"— 이벤트 리스크 존재, WATCH 조건 해당"
            )
        elif 4 <= days <= 14:
            earnings_lines.append(f"📅 실적 발표 예정: {ec['next_earnings_date']} (D-{days})")

    re_earn = ec.get("recent_earnings")
    if re_earn and re_earn.get("actual_eps") is not None:
        surprise = re_earn.get("surprise_pct")
        if surprise is not None:
            emoji = "🟢" if surprise > 0 else "🔴"
            label = "어닝 서프라이즈 (예상 상회)" if surprise > 0 else "어닝 쇼크 (예상 하회)"
            earnings_lines.append(
                f"{emoji} 최근 실적 ({re_earn['date']}): "
                f"EPS 실제 ${re_earn['actual_eps']} / 예상 ${re_earn['estimate_eps']} "
                f"({'+' if surprise > 0 else ''}{surprise}% — {label})"
            )

    rf = ec.get("recent_financials")
    if rf:
        parts = []
        if rf.get("revenue_b"):    parts.append(f"매출 ${rf['revenue_b']}B")
        if rf.get("net_income_b"): parts.append(f"순이익 ${rf['net_income_b']}B")
        if rf.get("op_income_b"):  parts.append(f"영업이익 ${rf['op_income_b']}B")
        if parts:
            earnings_lines.append(
                f"📊 최근 분기 ({rf.get('quarter', '')}): " + " / ".join(parts)
            )

    earnings_text = (
        "\n".join(earnings_lines)
        if earnings_lines
        else "실적 데이터 없음 (ETF이거나 yfinance 수집 실패 — 추측 금지)"
    )

    psr_ttm = val.get("psr_ttm")
    psr_forward = val.get("psr_forward")
    psr_runrate = val.get("psr_runrate")
    is_loss = val.get("is_loss_making", False)
    has_anomaly = val.get("has_gaap_anomaly", False)

    psr_parts = []
    if psr_ttm:
        psr_parts.append(f"TTM {psr_ttm}x")
    if psr_forward:
        psr_parts.append(f"Forward {psr_forward}x")
    if psr_runrate:
        psr_parts.append(f"Run-rate {psr_runrate}x")
    psr_text = "PSR: " + (" / ".join(psr_parts) if psr_parts else "—")

    loss_flag = (
        "⚠️ 적자 기업 (GAAP 흑자 시 일회성 항목 확인 필요)"
        if is_loss else ""
    )
    anomaly_flag = (
        "🚨 순이익 비정상 급증 감지 — 일회성 항목 포함 가능성"
        if has_anomaly else ""
    )
    exp_flag = ""
    if change_1m is not None:
        if change_1m > 50:
            exp_label = "극단적 기대 선반영 구간"
        elif change_1m > 20:
            exp_label = "기대 선반영 가능성 있음"
        else:
            exp_label = "기대 선반영 제한적"
        exp_flag = f"📈 최근 1개월 {change_1m:+.1f}% — {exp_label}"

    valuation_text = f"""
- PER: {_fv(val.get('per'), 'x')} (Forward: {_fv(val.get('forward_per'), 'x')})
- PBR: {_fv(val.get('pbr'), 'x')}
- {psr_text}
- EPS(GAAP): {_fv(val.get('eps'), '$') if val.get('eps') else '—'}
- 매출 성장률: {_fv(val.get('revenue_growth'), '% YoY')}
- 영업이익률: {_fv(val.get('profit_margin'), '%')}
- 섹터: {val.get('sector') or '—'}
{loss_flag}
{anomaly_flag}
{exp_flag}
""" if val else "밸류에이션 데이터 없음"

    ma200_text = (
        f"${stats['ma200']}"
        if stats.get('ma200')
        else "데이터 없음 (기간 부족)"
    )

    from volume_profile import format_volume_profile_for_prompt
    from signal_engine import format_engine_for_prompt
    vp_text = format_volume_profile_for_prompt(volume_profile)
    engine_text = format_engine_for_prompt(signal_engine)

    return f"""다음 주식을 분석해줘.

[분석 기준일: {analysis_date or "오늘"} — 반드시 이 날짜 기준으로만 분석할 것]
{sector_hint}
{FINANCIAL_RULE}
{expectation_rule}
{OUTPUT_RULE}

## 종목: {ticker}

### 현재 지표
- 현재가: ${stats['price']}
- 최근 5일 등락률: {_pct(stats.get('change_5d'))}
- 최근 20일 등락률: {_pct(stats.get('change_20d'))}
- 최근 1개월 등락률: {_pct(stats.get('change_1m'))}
- S&P500 대비 초과 수익: {_pct(stats.get('vs_spy'))}
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

### 매물대 (Volume Profile — 객관 계산값, 추측 금지)
{vp_text}

### Signal Engine 결과 (객관 — 덮어쓰기 금지, 설명만)
{engine_text}

### 밸류에이션
{valuation_text}

### 실적/어닝 컨텍스트 (yfinance 수집 — 이 데이터만 사용, 추측 금지)
{earnings_text}

### 최신 뉴스
{news_text}

---

차트 이미지를 보고 아래 항목을 분석해줘:

## 1. 전체 트렌드 분석
현재 추세(상승/하락/횡보), 주요 지지/저항 레벨(이동평균 + 매물대), 이동평균선 배열

## 2. 기술적 지표 해석
RSI, MACD, 볼린저밴드, 스토캐스틱 종합 해석

## 2.5 밸류에이션 분석
- PER/PBR이 섹터 평균 대비 고평가/저평가 여부
- 성장률 대비 밸류에이션 적정성 (PEG 관점)
- 현재 주가 수준의 밸류에이션 리스크

## 3. 거래량·매물대 분석
최근 거래량 추이, 평균 대비 수준, POC/HVN 위치, 지지↔저항 역할 전환 여부
(차트에 POC·지지/저항 매물 수평선이 표시됨)

## 4. 뉴스/이슈 영향
최신 뉴스가 주가에 미치는 영향

## 5. 단기 시나리오 (1~4주)
- 🟢 강세 시나리오: 조건과 목표가 (엔진 Expected Return 인용)
- 🔴 약세 시나리오: 조건과 주의 레벨 (Invalidation 포함)
- ENTRY / HOLDING / TRADING Action을 시나리오에 연결

## 6. 종합 의견
Trend vs Entry를 구분해 한 줄 요약 (예: 상승추세·진입대기로 추격 금지)

⚠️ 이 분석은 참고용이며 투자 결정은 본인 책임입니다.

⚠️ 반드시 ## 6. 종합 의견과 한 줄 요약까지 완성할 것. 중간에 끊기면 안 됨."""

async def analyze_with_claude(chart_b64: str, df: pd.DataFrame, ticker: str,
                              news_items: List[Dict], valuation: dict = None,
                              analysis_date: str = "",
                              earnings_context: dict = None,
                              signal_engine: dict = None) -> str:
    """Claude Vision API로 차트 + 뉴스 + 밸류에이션 + 어닝 종합 분석"""
    from volume_profile import compute_volume_profile
    stats  = get_summary_stats(df, ticker=ticker)
    vp = compute_volume_profile(df)
    prompt = build_analysis_prompt(ticker, stats, news_items, valuation,
                                   analysis_date=analysis_date,
                                   earnings_context=earnings_context,
                                   volume_profile=vp,
                                   signal_engine=signal_engine)

    try:
        message = _get_client().messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=6000,
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
