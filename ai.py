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
- 신호: BUY 또는 WATCH(상승 편향)

과열 국면 + 과대평가인 경우:
- RSI 80+ = SELL 신호로 작동
- 볼린저밴드 극단 이탈 = 단기 고점 신호
- 신호: SELL 또는 WATCH(하락 편향)

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

## STEP 3 — 시그널 판단 기준 (과감한 판단 원칙)

⚠️ 핵심 원칙:
WATCH는 가장 쉬운 답이지만 프로덕트 가치를 무너뜨린다.
확신이 55%여도 그 방향으로 BUY/SELL을 내려라. 단 CONFIDENCE로 신뢰도를 표시하라.
Transition 시장이라도 개별 종목은 BUY/SELL 판단 가능하다.

### BUY 조건 (2개 이상)
- RSI 30~65 + 상승 모멘텀 확인
- MACD 골든크로스 또는 히스토그램 플러스 전환
- 주요 지지선 근처 반등 (MA20/MA60 터치 후)
- 거래량 증가 동반 상승
- S&P500 대비 상대 강세 (+3% 이상 아웃퍼폼)

### SELL/비중축소 조건 (2개 이상)
- RSI 70 이상 + 하락 반전 신호 (데드크로스 임박 포함)
- 주요 이동평균 이탈 + 하락 추세 가속
- 거래량 감소 속 고점 형성
- S&P500 대비 상대 약세 (-3% 이상 언더퍼폼)
- 악재 뉴스 + 기술적 약세 동시 출현
- ETF는 SELL 대신 반드시 "비중 축소" 사용

### 실적 발표 직후 SELL 조건 (특수 케이스)
다음 조건 3개 이상 충족 시 WATCH 대신 SELL/비중축소 판단:
- 실적 발표 당일 또는 다음날
- 애프터마켓/프리마켓 갭 다운 -2% 이상
- 약세 시나리오 확률 60% 이상
- 거래량 미동반 반등 (평균 대비 -10% 이상 감소)

### WATCH 허용 조건 — 엄격히 제한
아래 3가지 중 하나에 해당할 때만 WATCH 사용:
1. 48시간 내 중대 이벤트 예정 (실적발표, FOMC, CPI)
2. 핵심 지지/저항선 ±1% 이내에서 방향 결정 직전
3. BUY/SELL 신호가 정확히 동수로 충돌하는 경우

### Transition 시장에서도 개별 종목 판단 가능
- Transition이라도 개별 종목이 명확한 하락/상승이면 SELL/BUY 가능
- "Transition이라 모른다" = WATCH 남용 금지
- 종목 자체 차트와 수치로 판단하라

### WATCH 출력 시 필수 포함
1. 현재 편향(Bias): 반드시 방향 명시
   예) "하락 편향 65% — $90 이탈 시 SELL 전환"
2. BUY 전환 트리거: 구체적 가격+조건
3. SELL 전환 트리거: 구체적 가격+조건
4. 예상 대기 기간: "2~3일 내 방향 확정 예상"

### 금지 표현
- ❌ "불확실하므로 관망"
- ❌ "시장 상황을 지켜볼 필요"
- ❌ "추세 확인 후 진입"
- ❌ 트리거 없는 WATCH

### 목표 비율
BUY 40% / SELL 30% / WATCH 30% — WATCH가 50% 넘으면 판단력 없는 것

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

3. 강세/약세 확률 차이 최소 20% 이상
   - 50:50은 진짜 혼재할 때만

4. 결론은 한 문장으로
   - ❌ "다양한 요인을 고려할 때 신중한 접근이 필요합니다"
   - ✅ "Bull Market + RSI 과매수 = 추격 금지, $610 눌림목에서 분할 매수"

## 출력 형식

분석 마지막에 반드시 아래 3줄 출력:

CONFIDENCE:상 또는 CONFIDENCE:중 또는 CONFIDENCE:하
SIGNAL:BUY 또는 SIGNAL:WATCH 또는 SIGNAL:SELL

WATCH 출력 시 반드시 추가:
WATCH_BIAS: 상승편향 XX% 또는 하락편향 XX%
WATCH_BUY_TRIGGER: 구체적 조건 (예: $XX 돌파 + 거래량 XX% 이상)
WATCH_SELL_TRIGGER: 구체적 조건 (예: $XX 이탈 + RSI XX 하회)
WATCH_DURATION: 예상 대기 기간 (예: 2~3일 내 방향 확정)

## 자기 검증 — 출력 전 반드시 체크

1. "비전문가도 할 수 있는 말인가?" → YES면 다시 작성
2. "WATCH를 선택했다면 BUY/SELL로 뒤집을 근거가 충분한가?"
3. "구체적 가격/수치 없이 추상적 표현만 있는가?" → YES면 다시 작성
4. "사용자가 이 분석으로 실제 행동할 수 있는가?" → NO면 다시 작성

전문 애널리스트의 가치는 뾰족한 의견에 있다.
확신도 55%여도 방향성을 제시하는 것이 프로페셔널의 의무다."""

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
5. 거래량 해석 원칙:
   → 거래량 증가 + 하락 = 매도 압력 강함 (위험)
   → 거래량 감소 + 하락 = 매도 압력 약함 (덜 위험)
   → 거래량 감소를 하락 신호로 쓰지 말 것
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
                          earnings_context: dict = None) -> str:
    news_text = "\n".join([
        f"- [{item['source']}] {item['title']}"
        for item in news_items[:8] if item.get("title")
    ]) or "뉴스 없음"

    val = valuation or {}
    def _fv(v, suffix=""):
        return f"{v}{suffix}" if v else "—"
    def _pct(v):
        return f"{v}%" if v is not None else "데이터 없음"
    change_1m = stats.get("change_1m")
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

    return f"""다음 주식을 분석해줘.

[분석 기준일: {analysis_date or "오늘"} — 반드시 이 날짜 기준으로만 분석할 것]

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

### 밸류에이션
{valuation_text}

### 실적/어닝 컨텍스트 (yfinance 수집 — 이 데이터만 사용, 추측 금지)
{earnings_text}

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
                              analysis_date: str = "",
                              earnings_context: dict = None) -> str:
    """Claude Vision API로 차트 + 뉴스 + 밸류에이션 + 어닝 종합 분석"""
    stats  = get_summary_stats(df, ticker=ticker)
    prompt = build_analysis_prompt(ticker, stats, news_items, valuation,
                                   analysis_date=analysis_date,
                                   earnings_context=earnings_context)

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
