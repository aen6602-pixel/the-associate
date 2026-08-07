# SKSQ Valuation Agent

자연어로 밸류에이션·기업분석 질문을 던지면, **검증된 공개 데이터 소스(API)** 를 골라
호출해서 답하는 에이전트. Capital IQ / Bloomberg 없이 할루시네이션 없는 분석을 목표로 한다.

예시 질문:
- "한국의 market risk premium은?"
- "SK Square의 상증법상 주당 가치는?"
- "무신사 대비 Peer들의 trading 멀티플은?"
- "특정 비상장사를 DCF 해줘" → DART 5개년 → P&L → 가정 → WACC → 가치

## 설계 원칙 (불변)

숫자는 **LLM 이 만들지 않는다.**

```
[UI]  Streamlit  ── 입력창 + 답변 박스 + "어떤 API 를 썼는지" 트레이스
[두뇌] Claude tool-use  ── 어떤 도구를, 어떤 인자로 부를지만 결정 (LLM)
──────────────────────────────────────────────────────────
[engines]  wacc / comps / 상증법 / dcf   ── 계산 (결정론 코드)
[providers] dart / ecos / fred / krx / damodaran / sec / edinet / finmind / fx
[core]      schema(=Provenance) / config / http
```

- 모든 수치는 `core.schema.Value` 로 감싸 **출처(Provenance)** 를 달고 다닌다.
- 데이터가 없으면 `DataError` 를 올린다 (조용히 0/None 반환 금지).

## 데이터 소스 (한/미/일/대만 + 글로벌)

| 축 | 🇰🇷 | 🇺🇸 | 🇯🇵 | 🇹🇼 | 글로벌 |
|---|---|---|---|---|---|
| 공시·재무 | DART | SEC EDGAR | EDINET | FinMind / MOPS | — |
| 주가·시총 | pykrx | yfinance | yfinance | FinMind | — |
| 무위험이자율 | ECOS | FRED | 財務省/FRED | CBC/FRED | — |
| ERP·산업베타·세율 | — | — | — | — | **Damodaran** |
| 매크로 | ECOS/KOSIS | FRED | BOJ | DGBAS | — |
| 환율 | — | — | — | — | frankfurter(ECB) |
| M&A·거래사례 | DART 평가의견서 | EDGAR DEFM14A | EDINET TOB | MOPS | — |

## 실행

```powershell
# 1) 키 설정
copy .env.example .env   # 값 채우기

# 2) 실행
.\.venv\Scripts\streamlit run app.py
```
