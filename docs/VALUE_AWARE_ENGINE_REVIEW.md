# Value-Aware Decision & Report Engine — 전문가 검토용 설계 요약

**문서 목적:** StockAI 분석 리포트/시그널 엔진 고도화안 검토  
**상태:** §10 스펙 기준 **구현 반영** (`signal_engine_v5` / `features_v2`)  
**근거 데이터:** MongoDB `analyses` 223건 + `signal_outcomes` 사후수익률 (2026-04 ~ 2026-10)  
**관련 코드(현행):** `signal_engine.py`, `signal_v4.py`, `signal_features.py`, `ai.py`, `index.html`

---

## 1. 문제 정의

### 1.1 제품 문제
현재 리포트는 사실상 **가격/추세 모멘텀을 BUY·SELL로 압축**하는 경향이 강하다.

- 가격↓ / 추세 약세 → SELL·EXIT
- 가격↑ / 추세 강세 → BUY (또는 관망)

사용자가 원하는 인사이트는 이와 다르다.

> 적정가까지 왔고 thesis(사업 가설)가 안 깨졌으면 **분할매수**  
> 너무 올랐으면 **오버슈팅·과매수이니 일부 분할매도**  
> 전량 청산은 **가설 훼손·구조적 지지 붕괴**일 때만

### 1.2 데이터로 확인된 증상 (전수조사)

| 관찰 | 수치/사실 | 해석 |
|------|-----------|------|
| 시그널 믹스 | WATCH ~70%, SELL 59, BUY 8 | 매수 기준 사실상 부재 (`buy_enabled=false`) |
| SELL @ RSI>70 | 5일 평균 수익률 **약 -7.2%** | **과열 구간 축소는 유효** |
| SELL @ 과매도/중립 RSI | 5일 평균 ≈ 0% 근처 | **하락 중 EXIT는 변별력 약함** |
| SELL 본문 | 59건 중 **36건**이 “과매도 + 청산” 동시 서술 | 과매도를 매도 근거로 오용 |
| 전역 False SELL | 5일 +10% 이상 **16%**, 20일 +10% 이상 **35%** | 매도 후 반등(기회비용) 큼 |
| BUY | n=8, 5일 적중 25%, 평균 -3.1% | 표본·성과 모두 약함 → 배포 비활성 |

**IREN 대표 아크**

| 시점 | 시그널 | 맥락 | 사후 | 판정 |
|------|--------|------|------|------|
| 04-28 BUY ~$44 | RSI~50 | 위치·가치형 매수 | 5d +24% / 20d +54% | 성공 |
| 05-07 SELL ~$64 | RSI~64 | 고점 부근 축소 | 이후 하락 | 유효 |
| 08-28 WATCH_DOWN ~$36 | 바닥 직전 | 약세만 표기 | 5d **+26%** | Missed scale-in |
| 10월 SELL/EXIT $41→$35 | Bottoming 26–37 | 하락 중 청산 반복 | 이후 ~$28 | 기계적 매도 (문제) |

### 1.3 현행 엔진의 구조적 한계
이미 v3/v4에서 Trend ≠ Entry, Bottoming(Oversold+Stabilization), chase_blocked 등은 도입했으나:

1. **Value Regime이 Action에 약하게 연결**됨 (멀티플 “FAIR”와 성장·수익성 괴리 미흡)
2. Softener가 `Bottoming≥60` 등으로 **너무 빡세** 실제 과매도 EXIT를 거의 못 막음
3. LLM이 엔진 EXIT를 **“하락 추세이므로 청산”** 내러티브로 증폭
4. 상위 KPI가 여전히 BUY/SELL 방향 적중률 중심 → **과열 REDUCE·바닥 SCALE_IN**을 보상하지 않음

---

## 2. 목표 아키텍처 (한 줄)

```
Features
  → Value Regime × Extension × Thesis × Spatial Support × MTF
  → Rule Gates (Hard EXIT 우선)
  → Action (BUY / SCALE_IN / HOLD / WAIT / REDUCE / EXIT)
     + Core vs Tactical sizing
  → LLM은 설명만 (모멘텀 단독 매도 문장 Validator 반려)
  → KPI는 Value-aware / Missed Scale-in / REDUCE hit 등
```

**폐기:** “내일 오를지” 예측을 최종 목표로 삼는 설계  
**채택:** 상태 분류 + 가치 위치 + 역할별(보유/신규/전술) 행동 인사이트

---

## 3. 핵심 의사결정 원칙 (불변)

1. **적정/저평가 + 과매도 + Thesis 유지** → `SCALE_IN` / `BUY`
2. **오버슈팅 + 과매수(과열)** → `REDUCE` (분할매도)
3. **단기 추세 약해도 Thesis·가치가 안 깨짐** → `HOLD` / 탐색 (**전량 EXIT 금지**)
4. **`EXIT`는 Hard만** — Thesis 훼손, 핵심 장기 지지(Major HVN) 완파 등

**절대 금지**
- 가격 하락 → 매도
- 가격 상승 → 매수  
(로직·프롬프트·UI 카피 모두)

---

## 4. Phase별 수정 범위

### P0 — 아키텍처·입출력 재정의
| 항목 | 내용 |
|------|------|
| 리포트 목표 | 예측 → 상태+가치+역할별 Action |
| 입력 추가 | `portfolio_state`(보유 비중, 평단가), `sector_rs` |
| 출력 스키마 | `state_summary` + `decision` + `rationale` (아래 §6) |

### P1 — Classifier (상태/가치 분리)
| 모듈 | 출력 | 비고 |
|------|------|------|
| **Value Regime** | UNDERVALUED / FAIR / OVERVALUED / BUBBLE | 성장·수익성 대비 멀티플. **MA/BB/RSI를 valuation 근거로 쓰지 않음** |
| **Extension** | OVERSOLD / NEUTRAL / OVERBOUGHT | Value와 완전 분리 (가격 이격·RSI·BB 등) |
| **Thesis** | INTACT / WARNING / BROKEN | 분기 노이즈 vs 구조적 훼손 분리 |
| Thesis WARNING | Idiosyncratic Risk | 시장/섹터 견조한데 종목만 급락 → RS 필터 |
| Thesis BROKEN 예 | 가이던스 ≤ -10% 하향, 구조적 마진 붕괴 등 | 규칙 기반 1차, NLP는 후순위 |
| **Spatial Support** | AT_HVN_SUPPORT / IN_LVN_VACUUM / AT_RESISTANCE | Volume Profile + 장기 VWAP |
| **MTF** | 장기 구조(주봉/MA200) vs 단기 이격(일봉) | 필드 분리 표기 |

### P2 — Decision 라우팅 (Gate 우선순위)

```
Gate 1 Hard Exit     → EXIT (ALL)
Gate 2 OVERSOLD      → SCALE_IN | WAIT | HOLD(탐색)
Gate 3 OVERBOUGHT    → REDUCE | REDUCE_TACTICAL
Gate 4 NEUTRAL/WEAK  → HOLD/탐색  (단기 약세만으로 매도 금지)
```

**Gate 1**  
- Thesis == BROKEN **또는** Major HVN 완파 → `EXIT`

**Gate 2 (OVERSOLD)**  
- Value ∈ {UNDERVALUED, FAIR} ∧ Thesis INTACT  
  - HVN 지지 ∧ 섹터 RS 정상 ∧ 비중 < Max Cap → **`SCALE_IN`**  
  - LVN 진공 / 나홀로 급락 / Cap 도달 → **`WAIT`**  
- Value ∈ {OVERVALUED, BUBBLE} → **HOLD/탐색** (섣부른 저점매수 금지)

**Gate 3 (OVERBOUGHT)**  
- Value 고평가/거품 → **`REDUCE`** (공격적)  
- Value 저평가/적정 ∧ Thesis INTACT → **`REDUCE_TACTICAL`** (Core 유지, Tactical만 축소)

**Gate 4**  
- Thesis INTACT → **HOLD/탐색** (이평 역배열·단기 하락만으로 SELL 생성 금지)

**리스크 가드**
- **Allocation Cap + 쿨다운:** 연속 SCALE_IN 시 종목 Max Cap, 직전매수 대비 -X% 또는 N일 경과 전 추가매수 제한
- **Core vs Tactical:** Thesis 유지 REDUCE는 Tactical 물량만
- **LVN 대기:** 과매도라도 지지 없는 진공 구간은 SCALE_IN 대신 WAIT

### P3 — LLM 출력 제어
필수 4 슬롯:
1. **가치-가격 괴리** — Value Regime vs Extension 분리 서술  
2. **Action & Sizing** — 예: “Tactical 30% 축소”, “2차 분할매수 +5%p”  
3. **Thesis Invalidation** — 뷰 폐기 숫자/이벤트 (Hard EXIT 기준선)  
4. **Next Trigger** — 다음 SCALE_IN/REDUCE 조건  

**Validator (반려)**  
- “주가 하락 추세이므로 매도”, “이평 역배열로 청산” 등 **가격 모멘텀 단독 매도 근거** 문장  
**필수**  
- 결론 문장의 주어 = Value Regime 상태 + Thesis 유지 여부

### P4 — KPI (BUY/SELL 적중률만 사용 금지)
| KPI | 정의 |
|-----|------|
| Value-aware False SELL | Thesis·가치 유지 상태에서 잘못된 매도/EXIT 비율 |
| Missed Scale-in | 적정/저평가+과매도인데 SCALE_IN 누락 후 반등 |
| Overbought REDUCE hit | 과열 구간 REDUCE 후 단기 조정 적중 |
| Premature Reduce Cost | REDUCE 후 지속 상승 시 놓친 수익 (임계 튜닝) |
| Drawdown Cushion Ratio | 고점 REDUCE 현금이 이후 MDD 방어에 기여한 비율 |
| Scale-in VWAP Efficiency | 분할매수 집행 단가 vs 조정구간 단순평균 |

---

## 5. 출력 JSON 스키마 (리포트/API 계약)

```json
{
  "ticker": "IREN",
  "state_summary": {
    "value_regime": "UNDERVALUED | FAIR | OVERVALUED | BUBBLE",
    "extension_state": "OVERSOLD | NEUTRAL | OVERBOUGHT",
    "thesis_status": "INTACT | WARNING | BROKEN",
    "spatial_support": "AT_HVN_SUPPORT | IN_LVN_VACUUM | AT_RESISTANCE"
  },
  "decision": {
    "action": "BUY | SCALE_IN | HOLD | WAIT | REDUCE | EXIT",
    "target_tranche": "CORE | TACTICAL | ALL",
    "sizing_guide": "예: Tactical 물량의 30% 축소 / 2차 분할매수 (비중 5%p 추가)"
  },
  "rationale": {
    "value_vs_price": "멀티플은 역사적 하단이나 단기 수급으로 과매도. 가격 하락을 매도 사유로 해석하지 않음.",
    "invalidation_condition": "차기 분기 OPM 15% 하회 또는 $XX Major HVN 주봉 종가 이탈 시 EXIT.",
    "next_trigger": "$YY 지지 확인 + 변동성 축소 시 2차 SCALE_IN."
  }
}
```

레거시 `BUY/SELL/WATCH_*`는 **호환 매핑층**으로만 유지하고, UI·리포트 1순위는 위 `decision.action`으로 한다.

---

## 6. 수정 대상 파일 (구현 시)

| 파일 | 역할 |
|------|------|
| `signal_features.py` | RS, VWAP/HVN·LVN, MTF 파생 |
| `signal_v4.py` 또는 신규 `signal_value.py` | Value/Thesis/Spatial classifier |
| `signal_engine.py` | Gate 1–4 라우팅, Cap/쿨다운, Core/Tactical |
| `ai.py` | 4슬롯 강제, 네거티브 Validator |
| `main.py` | portfolio 입력(옵션), 엔진 메타 저장 |
| `index.html` | 상태 그리드 + Action/Sizing/Invalidation/Trigger UI |
| `tests/test_value_aware_decision.py` | Gate·IREN 회귀·Validator |

---

## 7. 회귀·수용 기준 (구현 완료 정의)

1. **IREN-like 스냅샷** (극과매도 + Thesis INTACT + Value 저평가/적정 + HVN 또는 WAIT 사유)  
   - `action ∈ {SCALE_IN, WAIT, HOLD}`  
   - **`EXIT`/`SELL` 단독 결론 금지** (Hard 조건 없을 때)
2. **과열+고평가** → `REDUCE` (또는 REDUCE_TACTICAL), BUY 금지  
3. LLM 출력에 모멘텀 단독 매도 문장 있으면 Validator 실패  
4. KPI 파이프라인이 False SELL / Missed Scale-in을 리포트 가능

---

## 8. 명시적 비범위 (1차)

- 브로커 실거래·자동주문
- ATR 기반 풀 리스크버짓 최적화 (sizing_guide 문구는 포함)
- Fundamental 이벤트 완전 자동 NLP (규칙 기반 Thesis 훅 우선)
- 과거 156건 무엔진 리포트 재생성 백필 (선택)

---

## 9. 구현 순서 제안

```
P0 (스키마·입력) → P1 (Classifier) → P2 (Gates) → P3 (LLM/UI) → P4 (KPI)
```

각 Phase는 테스트 가능한 단위로 머지. 프로덕션 `buy_enabled` 재활성은 **Paper SCALE_IN/BUY KPI가 임계 충족할 때까지** 보류 권장.

---

## 10. §10 검토 이슈 — 확정 사양 (Implementation Specs)

> 아래는 전문가 검토 질문 6항에 대한 **확정 패치**. 구현 시 이 수치·규칙을 따른다.

### 10.1 Value Regime (`signal_value.py`) — Q1 확정

- **금지:** MA / BB / RSI 등 가격 보조지표를 Valuation에 혼합하지 않음.
- **Track A (1순위, 고변동·적자 성장주 포함):**  
  - 시총 PSR 대신 **Forward EV/Sales** (또는 EV/EBITDA).  
  - 판정 = **최근 1년 역사적 멀티플 Z-Score 50%** + **동일 섹터 Peer 중앙값 대비 상대 멀티플 50%**.
- **Track B (Fallback):** Forward 컨센서스 부재·적자만 지속 시  
  - **52주 고점 대비 Drawdown Percentile** + **200일 장기 VWAP 괴리율**을 Price-implied Value Proxy로 사용 (파이프라인 에러 방지).

### 10.2 Thesis Market-Implied Fast-Track — Q2 확정

실적(가이던스 -10% 등)만 기다리면 ATM·단가 급변 등 **주가 선반영 악재에 늦음** → 선행 룰 추가.

| 상태 | 조건 | 효과 |
|------|------|------|
| **WARNING** | 섹터 ETF/Peer 대비 **5일 RS ≤ −2.0σ** | Gate 2에서 **SCALE_IN 차단 → WAIT** |
| **BROKEN (선행)** | 5일 RS ≤ **−3.0σ** **AND** 20일 평균 대비 **거래량 ≥ 300%** 대량 **음봉** | 실적 전이라도 **즉시 BROKEN** (희석/이탈 등 구조 악재 가정) |

기존 실적 기반 BROKEN 규칙과 **OR**로 결합.

### 10.3 Spatial Support 파라미터 — Q3 확정 (`signal_features.py`)

| 항목 | 값 |
|------|-----|
| Lookback | **120거래일** rolling |
| Bins | 120일 High–Low 범위 **24개 균등** |
| **AT_HVN_SUPPORT** | 거래량 분포 **상위 30%** Node의 상·하단 **±0.5 ATR** 내 현재가 |
| **IN_LVN_VACUUM** | 거래량 분포 **하위 20%** 구간을 **하향 통과 중** → 과매도여도 **SCALE_IN 금지, WAIT** |
| **Major HVN** (Hard EXIT 기준) | 120일 내 최대 거래량 Node (**POC** 또는 하단 최대 HVN) |

### 10.4 Core / Tactical / Cap — Q4 확정 (`signal_engine.py`)

포트폴리오 미입력 시 Default:

| 항목 | 기본값 |
|------|--------|
| Core / Tactical | **70% / 30%** |
| REDUCE_TACTICAL 1회 | 전체 포지션 **10–15%p** 축소 가이드 (2~3회 후 Core 70% 홀딩) |
| 종목 Max Cap | 포트폴리오 **15%** 또는 동일 하락 사이클 **SCALE_IN 최대 3회** |
| SCALE_IN 쿨다운 | 직전 SCALE_IN 이후 **5거래일** 경과 **OR** 직전 시그널가 대비 **−1.5 ATR 또는 −7%** 추가 하락 전에는 연속 SCALE_IN 차단 → HOLD/WAIT |

### 10.5 Major HVN 완파 (Whipsaw 방지) — Q5 확정

하이브리드 종가 (장중/일봉 꼬리 stop-hunt 완화):

- **조건 A (주봉):** 금요일 **주봉 종가**가 Major HVN 하단을 명확히 이탈 마감  
- **조건 B (일봉 긴급):** **2거래일 연속** 일봉 종가가 Major HVN 하단 대비 **−1.0 ATR 이상** 깊게 이탈 마감  

→ **A 또는 B** 충족 시에만 Hard EXIT의 “구조적 지지 붕괴”로 판정.

### 10.6 레거시 매핑 & KPI 분리 — Q6 확정

**REDUCE와 EXIT를 DB에서 동일 SELL로 합치지 않음** (KPI 오염 방지).

| Action | 레거시 UI | DB `signal` / `signal_type` | KPI |
|--------|-----------|------------------------------|-----|
| BUY / SCALE_IN | BUY | BUY + `sub_action`: INITIAL \| SCALE_IN | — |
| HOLD / WAIT | WATCH | WATCH + `sub_action`: HOLD \| WAIT_SUPPORT | — |
| REDUCE / REDUCE_TACTICAL | **TRIM(비중축소)** 신설 권장 | **반드시 `signal_type=REDUCE`** (UI가 SELL이어도 DB 분리) | Overbought REDUCE hit / Premature Reduce |
| EXIT | SELL | SELL (전량 청산) | **Value-aware False SELL** 대상 |

---

## 11. 한 줄 결론 (검토용)

> StockAI는 “추세가 약하니 팔아라” 엔진이 아니라, **가치가 싼데 과매도면 나눠 사고, 거품·과열이면 나눠 줄이고, 가설이 깨질 때만 전량 나간다**는 Value-Aware 엔진으로 재설계한다.  
> §10 수치(EV/Sales Z+Peer, RS σ, HVN 120d/24bin, Core70, HVN 주봉·2일−1ATR, REDUCE≠SELL DB)까지 **구현 스펙으로 확정**했다.

---

*작성: StockAI 설계 논의 정리 · §10 패치 반영 · 구현 착수 전*
