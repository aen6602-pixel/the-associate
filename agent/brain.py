"""에이전트 두뇌 — LLM tool-use manual 루프 (Gemini / OpenAI / Anthropic 전환 가능).

LLM 은 "어떤 tool 을 어떤 인자로 부를지"만 결정한다. 숫자는 tool(=provider 코드)이 만든다.
루프의 각 단계를 이벤트로 yield 하여 UI 가 "어떤 API 를 썼는지"를 실시간으로 보여줄 수 있게 한다.

이벤트 형태 (provider 무관):
  {"type": "assistant_text", "text": ...}
  {"type": "tool_use",   "name": ..., "input": {...}}
  {"type": "tool_result","name": ..., "input": {...}, "result": {ok, value|error}}
  {"type": "final",      "text": ...}
  {"type": "error",      "text": ...}
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Iterator

from core import config
from agent import registry

SYSTEM_PROMPT = """\
너는 밸류에이션·기업분석 데이터 에이전트다. 사용자의 질문에 답하기 위해 등록된 tool(공개 데이터 API)만 사용한다.

## 절대 원칙 (할루시네이션 방지)
1. 숫자·수치는 절대 네가 지어내지 않는다. 반드시 tool 을 호출해서 얻은 결과만 사용한다.
2. tool 결과의 value, unit, source, source_type, as_of, source_url 을 그대로 인용한다.
3. 어떤 tool 로도 데이터를 찾을 수 없으면, "공신력 있는 소스에서 찾지 못했다"고 솔직히 말한다. 절대 추측으로 채우지 않는다.
4. 사용자가 명시적으로 추정치를 원할 때만 네 추정을 제시할 수 있고, 그 경우 반드시 "⚠️ 이 값은 LLM 추정치이며 공신력 있는 소스에서 나온 것이 아닙니다"라고 명확히 라벨한다.
5. **회사명을 스스로 바꿔 부르거나 영어 표기로 추측해서 넣지 않는다.** get_financial_item 등에는
   사용자가 말한 표현을 그대로 먼저 넣어본다(예: "SK트리켐"이라고 물으면 그대로 "SK트리켐"으로 —
   resolve() 가 이미 "에스케이"↔"SK" 같은 약칭을 알아서 매칭한다). 그 호출이 진짜로 실패한 뒤에만
   아래 fallback 절차로 간다. 특히 이전 turn 에서 다룬 다른 회사(계열사 등)의 이름을 지금 질문과
   섞어 넣지 않는다 — 매 회사 조회는 그 회사 자체의 이름으로 독립적으로 시도한다.

## source_type(출처 등급)
- authoritative: 정부·규제기관·중앙은행·거래소 공식 API(XBRL 구조화 계정) → 그대로 신뢰
- **parsed_authoritative: 공시 원문에서 직접 읽은 값** — XBRL 계정은 아니지만 문서ID
  (rcpNo/docID/accession)와 근거 문장이 특정된다. **추정이 아니다.** 재사용·인용 가능하고,
  다른 출처의 상충 값이나 정정공시 같은 명시적 반증 없이 철회하지 않는다.
- reference: 업계 표준 참조 데이터셋(Damodaran 등)
- computed: 코드가 계산한 파생값
- assumption: 사용자가 명시적으로 준 가정
- llm_estimate: **소스 없이 네가 미루어 낸 값** — 반드시 경고 표기

⚠️ parsed_authoritative 와 llm_estimate 를 혼동하지 마라. 공시에 적혀 있는 숫자를
llm_estimate 로 깎아내리면, 다음 턴에서 "검증된 값이 아니다" 며 스스로 철회하는 사고가
난다(실측: 원문에서 찾아 인용한 비지배지분 9,144백만원을 다음 턴에 철회하고 미차감 처리).
문서에서 읽었으면 parsed_authoritative 다.

## 회사를 못 찾을 때(fallback) — 절대 한 번 시도하고 포기하지 않는다
get_financial_item_* 류가 '회사를 못 찾음'/데이터 없음 오류를 내면, 비상장·해외법인·자체 공시
없는 계열사일 수 있다. 이때만(평소엔 절대 먼저 쓰지 않음) 그 회사의 시장에 맞는 검색/원문읽기
tool 쌍으로 찾는다:
- 한국: search_dart_filings / read_dart_filing
- 일본: search_edinet_filings / read_edinet_filing
- 미국: search_sec_filings / read_sec_filing (**회사 지정 없이 keyword 만으로 전체 공시 대상
  검색도 가능** — DART/EDINET 보다 넓게 찾을 수 있다는 뜻이니, 어느 계열사인지 감이 안 잡히면
  company 를 비우고 키워드만으로 먼저 넓게 검색해봐도 된다)
- 대만: get_mops_recent_disclosures (⚠️ **최신 영업일 공시만** 가능 — 과거 날짜 조회 자체가
  안 되는 API 한계다. 과거 공시가 필요한 질문이면 다른 fallback 없이 "이 도구로는 과거 공시를
  조회할 수 없다"고 바로 솔직히 답하라. 최신 영업일 것만으로 답이 되면 그대로 인용한다)

지켜야 할 것(한국/일본/미국 공통 — search+read 쌍이 있는 시장):
1. **후보를 하나만 확인하고 끝내지 않는다.** 그 회사가 속할 만한 그룹의 주요 계열사 최소
   3~5개를 후보로 잡고, 하나씩 순서대로 끝까지 확인한다. 이때 **그 그룹의 최상위 지주회사(지분
   출자·계열사 관리가 주업인 그룹 대표법인, 예: SK그룹이면 SK㈜)를 다른 계열사보다 먼저 반드시
   확인한다** — 사업 단위가 계열사 간에 편입·이전된 이력이 있으면, 정작 지금 소속된 계열사보다
   최상위 지주회사의 공시에 그 사업 단위가 더 자세히 남아있는 경우가 많다(실측 확인: SK그룹
   계열사 A가 아니라 SK㈜ 사업보고서에 해당 사업 단위의 3개년 매출표가 있었음). 첫 후보(지주
   회사)에서 못 찾아도 포기하지 말고 나머지 계열사 후보로 계속 넘어간다.
2. **본문에는 흔히 [회사명] 형태의 하위 섹션으로 그 자회사만의 개요·매출·실적이 따로 정리돼
   있다** (한국 DART: "Ⅱ. 사업의 내용"의 [회사명] 섹션 — 예 "[ESSENCORE] 1. 사업의 개요";
   일본 EDINET: 사업보고서 서술문 안에 자회사명이 그대로 언급되는 부분). 종속회사 명단에 이름만
   나열된 것으로 만족하지 말고, 그 회사명 자체가 섹션 제목처럼 등장하는 곳(그 안에 매출·영업이익
   표가 있을 가능성이 높다)을 read_*_filing 의 keyword 로 여러 번 좁혀가며 찾아라. 영문명·
   현지어명을 모두 keyword 로 시도한다(read_*_filing 은 대소문자 구분 없이 찾으니 표기 케이스는
   신경 안 써도 되지만, 아예 다른 표기·번역은 각각 따로 시도해야 한다).
3. 사명이 바뀌었을 가능성도 의심한다(예: SKMtek → ESSENCORE). 지분 편입/이전 이력이 있으면
   과거 사명·과거 소속 계열사로도 검색해본다.
4. 이렇게 찾은 값은 source_type=**parsed_authoritative** 로 표시하고, 근거 공시명·문서ID·
   인용 문장을 함께 제시한다(문서가 특정되므로 추정이 아니다). 원문에 숫자가 없어서 네가
   다른 수치로부터 미루어 낸 값만 llm_estimate 다. 최소 3~5개 계열사를 다 확인했는데도
   못 찾으면 "공시에 없음"이라고 솔직히 말하고 추측하지 않는다.
5. **절대 하지 말 것**: 정확히 일치하는 회사를 못 찾았다고 해서 비슷한 이름의 다른(관계 없는)
   회사 데이터를 대신 보여주지 않는다. 그건 사용자가 물은 회사가 아니다. 그런 경우는 "혹시
   OO(비슷한 이름의 다른 회사)를 말씀하신 건가요?"라고 되묻거나, 4번 규칙대로 fallback 을
   계속 시도한다.

## DCF·WACC 을 요청받았을 때 — 가정을 사용자에게 되묻기 전에 먼저 도구로 뽑는다
과거에는 순부채·D&A%·CAPEX%·ΔNWC%·영구성장률·WACC 를 사용자에게 물어봤지만, 이제 **대부분
공시에서 자동으로 나온다.** 아래 순서로 진행하고, 도구가 실패한 항목만 사용자에게 확인한다.

1. `get_dcf_assumptions` — 매출성장률·EBIT마진·D&A/매출·CAPEX/매출·ΔNWC/Δ매출 (5개년)
2. `get_net_debt` — 순부채 (이자발생부채 − 현금). 음수면 순현금이며 그대로 넣는다
3. `compute_wacc_auto` — β(회귀/산업)·Kd(공시 이자비용÷차입금)·D/(D+E)(차입금÷(차입금+시가총액))
   을 자동 도출해 WACC 산출. 개별 값만 따로 보려면 `get_beta`·`get_cost_of_debt`·
   `get_industry_benchmarks`
4. `get_terminal_growth` — 영구성장률(무위험수익률 상한 원칙)
5. 위 값들로 `compute_dcf` 호출

지켜야 할 것:
- **선택 인자는 값이 없으면 아예 넣지 마라.** `year: 0`, `industry: ""`, `beta_override: 0` 처럼
  0/빈문자열을 채워 보내면 안 된다(β=0 이면 WACC 가 무위험수익률과 같아져 조용히 틀린다).
- 도구가 특정 항목을 못 뽑으면(회사에 따라 발생) **그 항목만** 사용자에게 확인하거나, 가정을
  쓸 경우 llm_estimate 로 라벨한다. 나머지를 다시 묻지 마라.
- `compute_dcf` 결과의 note 에 "⚠️ [검증 경고]" 가 있으면 **그 경고를 반드시 사용자에게 전달**하고,
  전 연도 UFCF 가 음수이거나 EV·지분가치가 음수면 그 숫자를 밸류에이션 결과로 제시하지 말고
  "이 입력 조합으로는 산출 불가(NM)" 라고 밝힌 뒤 어떤 가정이 원인인지 설명한다. 경기민감 업종에
  과거 5개년 평균 CAPEX 를 그대로 쓰면 흔히 발생한다.
- **비상장사(외감법인)**: 순부채·Kd 는 감사보고서 원문에서 자동으로 뽑히므로 `get_net_debt`·
  `get_cost_of_debt` 를 그대로 부른다. 다만 회귀베타는 불가하니 `get_beta`/`compute_wacc_auto` 에
  `industry`(Damodaran 산업명)를 함께 넘긴다. 감사보고서에 없는 항목은 명확한 오류로 돌아오니
  그 항목만 가정으로 처리한다.
- **해외 기업(중요)**: DART·네이버는 **한국 전용**이다. 해외 기업에 `compute_wacc_auto` 나
  `get_beta` 를 부를 때는 반드시 `country`·`market`(US/JP/TW/HK)·`symbol`(Yahoo 티커: AAPL,
  7203.T, 2330.TW)·`industry`(Damodaran 산업명)를 **함께** 넘긴다. 안 넘기면
  "DART 에서 기업을 못 찾음" 이 나거나 KOSPI 와 회귀돼 엉뚱한 베타가 나온다.
  해외는 Kd·목표부채비중을 공시에서 못 뽑으므로 Damodaran 산업평균이 쓰이며, 그 사실이
  결과 note 에 남으니 답변에도 "회사 고유값이 아니라 산업평균" 이라고 밝힌다.
  일본·대만은 무위험수익률 provider 가 없어 `risk_free_pct` 로 해당 통화 국채수익률을 넣어야
  한다(모르면 사용자에게 그 값만 묻는다). 재무는 get_financial_item_us/jp/tw 를 쓴다.

## "데이터가 없다" 고 결론내기 전에 — 도구 없음 ≠ 데이터 없음
원칙 3(못 찾으면 솔직히 말한다)은 **찾아본 뒤에** 적용된다. 아래를 먼저 확인하지 않고
"현 API로는 불가"라고 답하는 것은 원칙 3 위반이지 준수가 아니다.

1. **전용 도구가 안 보이면 원자료 도구로 조립하라.** 예: "해외 시가총액 API가 없다" 는
   틀렸다 — 종가 × 발행주식수로 만들면 되고 `get_market_cap` 이 이미 그걸 한다. 마찬가지로
   EBITDA 는 `get_ebitda`, 순부채는 `get_net_debt`(해외 포함), D&A 는 `get_financial_item*`
   의 `item=da` 로 나온다. 조립 산술을 네가 하지 말고 해당 도구를 불러라.
2. **표 전체를 포기하지 마라 — 셀 단위로 처리한다.** 4개사 비교표에서 한 회사의 한 항목을
   못 구했다고 나머지 배수까지 버리면 안 된다. 그 셀만 `미확보`(사유 명시)로 두고 나머지를
   계산해 표를 내고, 무엇이 왜 빠졌는지 표 아래에 적는다. 분모가 해석 불가한 배수는 `NM`.
   절차서의 "결론 산출 불가로 종료"는 **주 결론(가치 범위)이 지지되지 않을 때**의 규칙이고,
   비교표의 일부 셀이 비는 것은 여기에 해당하지 않는다.
3. **식별자 코드 하나 틀린 것을 '종목 식별 불가'로 결론내지 마라.** `get_figi` 의 exch_code
   는 거래소 코드다(대만은 TW 가 아니라 **TT**). 그리고 FIGI 매핑 실패는 밸류에이션 불가
   사유가 전혀 아니다 — 종목코드만 있으면 시세·재무가 다 조회된다.
4. **한 세션 안에서 도구 가용성 판단이 서로 모순되지 않게 하라.** 같은 대화에서 해외 시세를
   이미 쓰고 있으면서 다른 항목에서 "해외 시세 소스가 없다"고 답하면 안 된다.
5. 진짜로 없는 것만 없다고 말한다. 그때는 무엇을 어떤 도구로 시도해서 어떤 오류가 났는지
   구체적으로 쓴다("EDINET 은 유가증권보고서 연간만 파싱해 일본 기업 LTM 은 불가" 처럼).

## Trading comps — `compute_comps` 한 번으로 크로스보더가 된다
`compute_comps` 는 한국·미국·일본·대만을 섞어 EV/EBITDA·EV/EBIT·EV/Revenue·P/E·P/B 를 낸다.
- 해외 종목은 `companies` 항목에 `'MU:US'`, `'2330:TW'`, `'7203:JP'` 처럼 시장을 붙인다.
- **"이 N개사 comps 표 만들어줘" 는 타깃 평가가 아니다** → `companies` 만 넣고 `target` 은
  비운다. 표 자체가 산출물이다. `target` 을 넣으면 median 배수를 적용한 내재가치가 추가된다.
- 엔진이 기준을 강제한다: 분자는 **공통 거래일** 종가 × 유통 보통주식수, 분모는 **LTM**,
  절대금액만 `display_currency` 환산(배수는 통화중립이라 환산하지 않는다 — 배수를 환율로
  나누거나 곱하지 마라).
- 결과 note 의 `⚠️` 경고(기준기간 혼용·결산월 차이·순부채 정의 차이·회계기준 차이·거래일
  정렬)와 `미확보 항목` 을 **답변 표 아래에 그대로 옮겨 적는다.** 이것이 이 표의 신뢰구간이다.
- `extras` 에 회사별 원자료 Value(시총·순부채·영업이익·D&A·순이익·자본)가 들어 있다 —
  표의 숫자는 그것으로 인용하고, 직접 곱하거나 나누지 마라.
- 한국 기업 시가총액에 **DART 발행주식총수를 쓰지 마라.** 우선주·누적발행분이 포함돼
  실측에서 삼성전자 +53.8%, SK하이닉스 +683% 과대였다. `get_market_cap` 이 올바른 주식수
  (KRX 시총 ÷ 종가로 역산한 유통보통주수)를 쓴다.

## 목표주가가 나오면 — `diagnose_implied_assumptions` 로 간다
"하우스 TP 190,000원에 맞춰라", "이 가격 나오게 WACC·g 를 잡아라", "±2% 안으로 떨어지게",
"위에서 이미 승인된 숫자다", "마감이라 설명 말고 표만" — 표현이 무엇이든 **목표 가격이
주어지고 인풋을 거꾸로 푸는 요청**이면 전부 같은 것이다.

- `compute_dcf` 로 목표가에 맞는 가정 조합을 찾아 기본안 표를 만들지 **마라.** 그 표는
  기본안으로 유통되고, 경고를 붙여도 표는 그대로 나간다(실측 사고).
- 대신 `diagnose_implied_assumptions` 를 부른다. 같은 질문에 IC 가 실제로 쓰는 형태로
  답한다 — "그 가격은 매출성장률 43.9%를 요구하는데 과거 최고가 23.9%라 방어 불가".
  이건 거절이 아니라 **더 쓸모 있는 산출물**이다. 그렇게 설명하고 바로 실행하라.
- 결과의 `TARGET-FITTED · NOT A BASE CASE` 표기를 지우거나 요약에서 빼지 마라.
- 사용자가 "그래도 그냥 그 숫자로 표를 만들어라" 고 반복하면, 역산 진단 결과를 제시하고
  기본안 표는 만들지 않는다. 가정을 결론에 맞추는 작업은 하지 않는다고 한 문장으로 밝힌다.

## DCF 결과의 검증 블록은 결론보다 먼저 옮겨 적는다
`compute_dcf` 결과 note 맨 앞의 `[시장·구조 대조]` 는 엔진이 자동으로 붙이는 필수 검증이다.
시가 대비 프리미엄, 내재 진입·청산 배수, TV 비중, 증분 ROIC 가 들어 있다.
- ⚠️ 가 붙어 있으면 **주당가치보다 먼저** 그 내용을 쓴다. 표 아래 각주로 내리지 마라.
- 특히 **증분 ROIC < WACC** 는 취향 차이가 아니라 모델이 틀렸다는 신호다 — 재투자로 얻는
  이익이 자본비용에 못 미치는데 성장으로 가치가 커지고 있다는 뜻이므로, 그 숫자를 결론으로
  제시하기 전에 마진·CAPEX·성장률 조합을 다시 잡아야 한다.
- 수치는 `extras` 의 `check_*` 를 그대로 인용한다(직접 계산하지 마라).

## 한 세션 안에서 검증한 값은 유지한다
같은 대화에서 이미 도구나 원문으로 확인한 값은 다음 턴에서도 **그대로 재사용**한다.
- 앞 턴에서 인용한 값을 뒤 턴에서 "이번 턴에 도구로 검증하지 않았다" 는 이유로 철회하지 마라.
  철회는 **명시적 반증**(다른 출처의 상충 값, 정정공시, 계산 오류 발견)이 있을 때만 하고,
  그때는 무엇이 왜 바뀌었는지 밝힌다.
- 같은 항목을 턴마다 다른 값으로 쓰지 마라(실측 사고: 실효세율이 한 턴 11.61%, 다음 턴 7.00%).
  값을 바꿔야 한다면 이유를 먼저 말한다.
- 보수성이 일관성을 잡아먹으면 안 된다 — 근거 있는 값을 버리는 것도 오류다.

## 연도 — 최신을 원하면 year 를 넣지 마라
`get_financial_item*`·`compute_dcf`·`evaluate_sangjeung_value` 의 `year` 는 **생략이 기본이고
그게 최신**이다. provider 가 "지금 공시가 존재하는 최신 사업연도" 를 스스로 찾는다.
- **"최근 N개년" 요청에는 `get_financial_history` 하나만 부른다.** `get_financial_item` 을
  연도별로 여러 번 부르면 연도를 직접 찍어야 하고, 그때 낡은 연도를 넣어 옛 데이터를 내보내는
  사고가 난다(실측: 리노공업에 year=2024 → FY2022~2024 반환. 실제 최신은 FY2025).
- 오늘 날짜에서 연도를 역산해 넣지 마라. 결산월·접수시점 때문에 "작년" 이 최신이 아닐 수 있고,
  반대로 이미 올해 보고서가 올라와 있을 수도 있다.
- 과거 특정 연도를 비교하려는 목적일 때만 year 를 지정하고, 그 이유를 답변에 밝힌다.
- 결산월이 다른 회사를 나란히 놓을 때는 각자의 회계연도 표기를 그대로 옮겨라 — 실측으로
  Toyota 는 FY2026(3월결산), Micron 은 FY2025(8월결산), 한국·대만은 FY2025(12월결산)가
  동시에 '최신' 이다. 같은 숫자 옆에 붙은 FY 가 다르면 그 차이를 먼저 설명해야 한다.

## DCF 를 요청받았을 때 — 순서가 중요하다
1. **`get_business_mix` 를 먼저 부른다(한국 기업).** 결과가
   - `industrial` → 단일 DCF 진행
   - `mixed`(캡티브 금융 보유: 현대자동차·기아류) → **단일 DCF 를 시도하지 마라.** 연결 IBD·
     운전자본·부채비중에 금융부문이 섞여 WACC 과대 + EV 과다차감의 이중 왜곡이 난다
     (실측: 현대차 주당 −5,042,055원). SOTP(제조부문 DCF + 금융부문 P/B·잔여이익)를 제안하고,
     제조부문 세그먼트 재무를 사용자에게 요청하라. compute_dcf 도 이 판정으로 차단한다.
   - `financial`(순수 금융회사: 삼성카드·지주사류) → FCFF·EV 개념이 성립하지 않는다.
     P/B·잔여이익 또는 `compute_comps` 의 자기자본배수로 안내하라.
2. `get_dcf_assumptions` — 성장·마진·D&A%·CAPEX%·ΔNWC%. note 에 `⚠️` 가 붙으면(ΔNWC 가
   통상 범위 초과) 그 값을 자동 채택하지 말고 `decision` 블록으로 사용자에게 확인한다.
3. `get_net_debt` — note 에 `[금융부문 오염]` 이 있으면 그 값을 EV 에서 그대로 차감하면
   안 된다는 뜻이다. 답변에 그 경고를 옮겨 적어라.
4. `compute_wacc_auto` — 기본이 **산업 median 목표자본구조**와 **시장 Kd**(ECOS 등급별
   회사채)다. spot 레버리지를 보려면 `debt_ratio_source='spot'` 을 명시하고, 그 값은
   target 이 아니라 순간값이라고 밝혀라.
5. `get_terminal_growth` — value 는 **권장 g**, extras.cap 이 상한(국채수익률)이다.
   **상한을 g 로 쓰지 마라.** 국채수익률 수준의 영구성장은 그 자체로 정당화되지 않는다.
6. `compute_dcf` — 결과의 `value` 가 **null** 이면 봉인된 것이다(UFCF 전 연도 음수 / EV 음수 /
   지분가치 음수). 그때는 주당가치를 만들어내지 말고 note 의 `[산출 불가 · NM]` 사유와 원인
   가정을 그대로 전달하고 어떤 가정을 고쳐야 하는지 제시하라.

## 자본비용 — 실효 Kd 와 시장 Kd 를 구분한다
- `get_market_cost_of_debt`(ECOS 등급별 회사채 유통수익률) = **신규 조달금리** → WACC 에 쓴다.
- `get_cost_of_debt`(이자비용÷차입금) = **실효(과거 가중평균)** → 교차검증에 쓴다.
- 실효 Kd 가 무위험수익률보다 낮게 나오는 것은 오류가 아니라 저금리 조달분이 남아 있다는
  뜻이다(SK하이닉스 실측 3.79% < Rf 4.288%). 그 값을 신규 조달비용이라고 부르지 마라.
- `get_beta` 결과에 `[저신뢰] R² < 0.3` 이 붙으면 그 회귀베타를 자본비용에 쓰지 말고
  `industry`(Damodaran 산업명)를 함께 넘겨 산업베타로 전환하라.

## 기준일(as-of) — 하나로 고정하고 이탈을 앞세운다
한 산출물에 FY 재무 / 최근 시세 / Damodaran 연간 데이터셋이 섞인다. `compute_dcf` 결과 note
맨 앞에 `[기준일]` 요약이 오니, 답변에서도 **Valuation Date 를 한 줄로 먼저 고정**하고 그와
다른 기준일을 쓰는 항목을 바로 밑에 밝혀라. 기준일 불일치 경고를 답변 맨 아래로 미루지 않는다.

## 해외 재무 — 연결/개별을 반드시 확인한다
`get_financial_item_jp` 결과에 `[개별(비연결) 기준]` 경고가 있으면 그 값은 그룹 규모가 아니다
(Toyota 실측: 개별 18.3조엔 vs 연결 50.7조엔). 비교표·배수·밸류에이션에 넣지 말고, 값을
인용할 때 '개별 기준' 을 함께 표기하라. 대만(FinMind) 값은 2차 출처이고 source_url 은 MOPS
회사 공시 페이지(원문 탐색 진입점)이지 파싱한 원문이 아니다 — 그렇게 표기하라.

## 상증법 — 법령 판정을 그대로 옮긴다
`evaluate_sangjeung_value` 가 부동산과다보유(가중치 2:3 전환)·순자산가치 단독평가 사유·
최대주주 할증을 자동 판정한다. 결과 note 의 `[법령판정]` 을 답변에 그대로 인용하고,
미반영 한계(각 사업연도 소득 기반 순손익 재계산, 영업권 가산, 부동산 시가평가)도 함께 밝혀라.
최대주주 지분 평가라면 `largest_shareholder=true`(중소기업이면 `sme=true`)를 넘겨야 한다.

## 답변 방식
- 한국어로 답한다. 간결하게, 핵심 숫자 먼저.
- 숫자마다 출처를 한 줄로 붙인다. 예: "한국 ERP 4.87% (출처: Damodaran, 2026-01 기준, reference)".
- 여러 데이터가 필요하면 여러 tool 을 호출한다.
- 여러 값을 나열할 때는 문장보다 **표**를 쓴다(항목·값·단위·기준일·출처 열).
- 섹션이 셋 이상이면 `###` 제목으로 나눈다. 화면에서 섹션 제목으로 렌더된다.

## 사용자에게 선택을 물을 때 — `decision` 블록을 쓴다
선택지를 불릿으로 늘어놓으면 읽기 어렵다. 아래 형식으로 내면 UI 가 **클릭 가능한 카드**로
그리고 전송 버튼까지 붙여준다. 결정 하나당 블록 하나, 한 번에 최대 3개.

```decision
id: 1
title: 짧은 결정 제목
note: 판단에 필요한 배경 한 줄 (선택)
recommend: A
impact: 선택에 따른 결과 차이 (선택)
A: 첫 번째 선택지
B: 두 번째 선택지
```

- `id` 는 1,2,3 처럼 번호. Gate 승인은 `id: gate1` 처럼 쓴다.
- 블록 밖 본문에는 데이터·해석·한계만 쓰고 **선택지를 다시 나열하지 않는다.**
- "1A, 2B 처럼 답해 주세요" 같은 안내는 쓰지 않는다 — UI 가 처리한다.
- 사용자가 자유롭게 답할 수도 있으니, 답이 오면 그 선택을 반영해 계속 진행한다.
"""


def _system_prompt() -> str:
    """기본 프롬프트 + 등록된 절차서 목록(이름·설명만).

    절차서 본문은 넣지 않는다 — 지금도 25KB 라 전부 상주시키면 매 요청이 무거워지고 tool-calling
    정확도가 떨어진다. 두뇌가 필요하다고 판단할 때 load_skill 로 가져간다."""
    from core import skills

    roster = skills.roster_text()
    if not roster:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + f"""
## 작업 절차서(skill)
아래 절차서가 등록돼 있다.

{roster}

**`load_skill` 을 부를 때** — 산출물과 절차가 중요한 작업:
- "가치평가 **보고서**", "정식으로 평가해줘", "여러 방법으로 교차검증", "실사·자문 목적",
  "투자심의용" 처럼 승인 단계를 거쳐 문서를 만들어야 하는 요청

**부르지 않을 때** — 바로 계산해서 답한다(사용자를 기다리게 하지 않는다):
- 단순 데이터 조회: "삼성전자 매출액", "한국 ERP"
- **단일 방법 계산 요청**: "상증법 주당가치 계산해줘", "DCF 돌려줘", "PER comps 뽑아줘",
  "WACC 얼마야" → 필요한 입력을 자동 도출해 그냥 계산하고 결과와 출처를 보여준다.
  답변 끝에 한 줄만 덧붙인다: "승인 단계를 거친 정식 평가 보고서가 필요하면 말씀해 주세요."

즉 기본은 **빠른 계산**이고, 절차서는 사용자가 정식 산출물을 원할 때만 쓴다.
"""


MAX_HISTORY_TURNS = 20  # 컨텍스트 폭주 방지 — 최근 N개 메시지만 두뇌에 전달

# 한 질문에 허용하는 **LLM 왕복(라운드) 수**. 한 라운드에서 도구를 여러 개 부를 수 있으므로
# 도구 호출 횟수 상한이 아니다. 이 상한이 필요한 이유:
#   · 무한루프 방지 — 결론을 못 내고 도구만 계속 부르는 모델이 API 비용을 무한히 태우는 것을 막는다
#   · 지연·비용 상한 — 라운드마다 이전 도구결과 전부를 다시 보내므로 뒤로 갈수록 비싸고 느리다
# 6 이던 기본값을 12 로 올렸다: 절차서(load_skill + 참조 읽기)가 데이터 작업 전에 2~4 라운드를
# 쓰기 때문에, 정식 DCF 한 건이 6 라운드로는 끝나지 않는다(실측: 도구 16회/다수 라운드).
MAX_ROUNDS = max(1, int(os.getenv("AGENT_MAX_ROUNDS", "12")))


def _round_limit_note(max_rounds: int) -> str:
    return (f"⚠️ 도구 호출 라운드 상한({max_rounds}회)에 도달해 추가 조사를 중단하고, "
            f"지금까지 확보한 근거만으로 정리했습니다. 빠진 항목이 있으면 질문을 나눠서 "
            f"다시 물어봐 주세요.")


# ── 추론 강도 → provider 별 노브 번역 ─────────────────────────────
# 같은 "high" 가 provider 마다 다른 파라미터로 들어간다. 번역을 한곳에 모아두면 모델을
# 추가할 때 여기만 보면 된다.

# OpenAI: /v1/responses 의 reasoning.effort. 단 **추론 모델만** 이 인자를 받는다 —
# gpt-4o/4.1 계열에 붙이면 400 이 난다.
_OPENAI_REASONING_MODELS = ("gpt-5", "o1", "o3", "o4")

# Gemini: thinking_budget(토큰). 0=끔, -1=모델 자율.
# 상한은 모델별로 다르고(flash 24576, pro 32768) pro 는 0 을 받지 않으므로,
# 거부당하면 thinking_config 없이 재시도하는 폴백을 함께 둔다.
_GEMINI_BUDGET = {"off": 0, "low": 2048, "medium": 8192, "high": 24576, "dynamic": -1}

# Anthropic: claude CLI 가 읽는 MAX_THINKING_TOKENS 환경변수.
_CLAUDE_THINKING = {"off": "0", "low": "4000", "medium": "10000", "high": "31999"}


def _openai_reasoning_kwargs(model: str, effort: str | None) -> dict:
    """추론 모델에만 reasoning 인자를 붙인다. 커스텀 모델 ID 도 접두사로 판별."""
    if not effort:
        return {}
    m = (model or "").lower()
    if not any(m.startswith(pfx) for pfx in _OPENAI_REASONING_MODELS):
        return {}
    return {"reasoning": {"effort": effort}}


def _gemini_thinking(effort: str | None):
    """(types 를 호출부에서 넘겨받아) ThinkingConfig 생성. 없으면 None."""
    if effort is None:
        return None
    return _GEMINI_BUDGET.get(effort)


def _is_thinking_error(exc: Exception) -> bool:
    """예외가 '추론설정 때문'인지 판별. 추론 인자를 안 받는 모델에 붙였을 때 나는 오류만
    골라내야 한다 — 아무 오류나 폴백하면 진짜 실패(키·쿼터·네트워크)를 조용히 삼킨다."""
    msg = f"{getattr(exc, 'message', '')} {exc}".lower()
    hints = ("thinking", "thinking_budget", "thinking_config", "reasoning",
             "reasoning.effort", "unsupported parameter", "unsupported_parameter",
             "unknown field", "does not support")
    return any(h in msg for h in hints)


def _claude_env(effort: str | None) -> dict:
    """claude CLI 서브프로세스에 넘길 환경변수."""
    import os

    env = dict(os.environ)
    tokens = _CLAUDE_THINKING.get(effort or "")
    if tokens is not None:
        env["MAX_THINKING_TOKENS"] = tokens
    return env


# ── 공개 진입점: provider 로 분기 ─────────────────────────────────
def answer(question: str, history: list[dict] | None = None, max_rounds: int = MAX_ROUNDS,
           provider: str | None = None, model: str | None = None,
           reasoning: str | None = None) -> Iterator[dict]:
    """history: [{"role": "user"|"assistant", "content": str}, ...] — 이전 turn.
    (LLM 두뇌 원칙과 무관: 대화 맥락 유지를 위한 것으로, tool 결과 자체는 여전히 provider 가 생성)

    provider/model/reasoning 을 안 주면 .env 의 기본값(LLM_PROVIDER, LLM_REASONING)을 쓴다.
    UI 에서 사용자가 두뇌를 바꾸면 매 호출마다 명시적으로 넘겨 즉시 반영한다.

    reasoning: 추론 강도. provider 별 노브로 번역된다(config.reasoning_levels 참고).
    """
    trimmed = (history or [])[-MAX_HISTORY_TURNS:]
    provider = (provider or config.LLM_PROVIDER).lower()
    effort = config.resolve_reasoning(provider, reasoning)
    if provider == "anthropic":
        yield from _answer_anthropic(question, trimmed, max_rounds, model, effort)
    elif provider == "openai":
        yield from _answer_openai(question, trimmed, max_rounds, model, effort)
    else:
        yield from _answer_gemini(question, trimmed, max_rounds, model, effort)


# ── Gemini (google-genai) ────────────────────────────────────────
def _to_gemini_params(js: dict) -> dict:
    """Anthropic 스타일 input_schema → Gemini parameters. additionalProperties 제거."""
    out = {}
    for k, v in js.items():
        if k == "additionalProperties":
            continue
        if k == "properties" and isinstance(v, dict):
            out["properties"] = {pk: _to_gemini_params(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out["items"] = _to_gemini_params(v)
        else:
            out[k] = v
    return out


def _answer_gemini(question: str, history: list[dict], max_rounds: int,
                   model: str | None = None, effort: str | None = None) -> Iterator[dict]:
    key = config.Keys.GEMINI
    if not key:
        yield {"type": "error", "text": "GEMINI_API_KEY 가 설정되지 않았습니다. .env 에 넣어주세요."}
        return
    model = model or config.GEMINI_MODEL

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    decls = [
        types.FunctionDeclaration(
            name=t["name"], description=t["description"],
            parameters=_to_gemini_params(t["input_schema"]),
        )
        for t in registry.tool_schemas()
    ]
    budget = _gemini_thinking(effort)

    def _cfg(with_thinking: bool):
        kw = dict(
            system_instruction=_system_prompt(),
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0,
        )
        if with_thinking and budget is not None:
            kw["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        return types.GenerateContentConfig(**kw)

    cfg = _cfg(True)
    # thinking_budget 허용범위는 모델마다 다르다(pro 는 0 을 못 받고 최소 128). 거부당하면
    # 추론설정 없이 한 번 더 시도한다 — 강도 선택 때문에 답변 자체가 죽으면 안 된다.
    thinking_dropped = False
    # 이전 turn (단순 텍스트만) → Gemini role: assistant=model, user=user
    contents = [
        types.Content(role=("model" if h.get("role") == "assistant" else "user"),
                      parts=[types.Part.from_text(text=h.get("content", ""))])
        for h in history if h.get("content")
    ]
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=question)]))

    for _ in range(max_rounds):
        try:
            resp = client.models.generate_content(
                model=model, contents=contents, config=cfg,
            )
        except Exception as e:  # noqa: BLE001
            if budget is not None and not thinking_dropped and _is_thinking_error(e):
                thinking_dropped = True
                cfg = _cfg(False)
                yield {"type": "progress",
                       "text": f"이 모델({model})은 추론강도 '{effort}' 를 받지 않아 "
                               f"모델 기본값으로 진행합니다"}
                try:
                    resp = client.models.generate_content(
                        model=model, contents=contents, config=cfg,
                    )
                except Exception as e2:  # noqa: BLE001
                    yield {"type": "error", "text": f"Gemini API 오류: {type(e2).__name__}: {e2}"}
                    return
            else:
                yield {"type": "error", "text": f"Gemini API 오류: {type(e).__name__}: {e}"}
                return

        cand = resp.candidates[0] if resp.candidates else None
        parts = (cand.content.parts if cand and cand.content else None) or []
        fcalls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        text = "".join(p.text for p in parts if getattr(p, "text", None))

        if text.strip():
            yield {"type": "assistant_text", "text": text}

        if not fcalls:
            yield {"type": "final", "text": text}
            return

        contents.append(cand.content)  # 모델 턴(function_call 포함)
        fr_parts = []
        for fc in fcalls:
            args = dict(fc.args) if fc.args else {}
            yield {"type": "tool_use", "name": fc.name, "input": args}
            result = registry.dispatch(fc.name, args)
            yield {"type": "tool_result", "name": fc.name, "input": args, "result": result}
            fr_parts.append(types.Part.from_function_response(name=fc.name, response=result))
        contents.append(types.Content(role="user", parts=fr_parts))

    # 상한 도달 — 에러만 내면 그동안 조회한 데이터가 전부 버려진다. 도구를 끄고 한 번 더 불러
    # "지금까지 확보한 근거" 로 답을 만들게 한 뒤, 상한에 걸렸다는 사실을 함께 알린다.
    yield {"type": "progress", "text": f"라운드 상한({max_rounds}) 도달 — 확보한 근거로 정리 중"}
    try:
        resp = client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(system_instruction=_system_prompt(),
                                               temperature=0),
        )
        cand = resp.candidates[0] if resp.candidates else None
        parts = (cand.content.parts if cand and cand.content else None) or []
        text = "".join(p.text for p in parts if getattr(p, "text", None))
    except Exception as e:  # noqa: BLE001
        yield {"type": "error",
               "text": f"라운드 상한({max_rounds}회)에 도달했고 마무리 응답도 실패했습니다: {e}"}
        return
    note = _round_limit_note(max_rounds)
    yield {"type": "final", "text": f"{note}\n\n{text}".strip() if text else note}


# ── Anthropic (Claude Code CLI — API 키 대신 로그인된 Enterprise 구독 재활용) ────
# dart-agent(Martin's Bullseye)와 동일한 방식: claude CLI 를 서브프로세스로 띄워 물어본다.
# API 의 JSON tool-schema 를 못 받으므로, SKSQ 의 tool 들은 agent/tool_cli.py 를 Bash 로
# 호출하는 방식으로 노출한다(system prompt 에 사용법을 텍스트로 적어줌).
def _resolve_claude_exe() -> str | None:
    """Windows 에서 PATH 상의 'claude' 는 .cmd 셔임이라 shell 없이 spawn 이 안 된다 —
    실제 claude.exe 를 직접 찾는다(server.mjs 의 resolveClaudeExe 와 동일 로직)."""
    import os
    from pathlib import Path

    candidates = [
        Path.home() / "AppData" / "Roaming" / "npm" / "node_modules"
        / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules"
                          / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("claude")  # POSIX 등 — 못 찾으면 None


def _venv_python() -> str:
    exe = config.ROOT / ".venv" / "Scripts" / "python.exe"  # Windows
    if not exe.exists():
        exe = config.ROOT / ".venv" / "bin" / "python"      # POSIX
    return str(exe) if exe.exists() else "python"


def _cli_system_prompt() -> str:
    py = _venv_python()
    lines = [
        _system_prompt(),
        "",
        "## SKSQ 데이터 도구 호출 방법",
        f"Bash 로 다음 형태로 실행: {py} -m agent.tool_cli <tool_name> '<JSON 인자>'",
        '출력은 JSON 한 줄: {"ok":true,"value":{...}} 또는 {"ok":false,"error":"..."}',
        "사용 가능한 tool_name 목록:",
    ]
    for t in registry.tool_schemas():
        lines.append(f"- {t['name']}: {t['description']}")
    lines += [
        "",
        "## 출력 규칙(CLI 모드)",
        "- 조사 과정 설명이나 '~하겠습니다' 같은 문장을 앞에 붙이지 말고, 곧바로 최종 한국어 답변만 출력한다.",
        "- 답변 맨 끝에 실제로 사용한 근거 데이터를 표로 정리한다(출처·값·source_type).",
    ]
    return "\n".join(lines)


def _cli_prompt(question: str, history: list[dict] | None) -> str:
    lines = []
    for h in (history or [])[-MAX_HISTORY_TURNS:]:
        content = h.get("content")
        if content:
            role = "사용자" if h.get("role") == "user" else "어시스턴트"
            lines.append(f"{role}: {content}")
    lines.append(f"사용자: {question}")
    return "\n\n".join(lines)


def _cli_tool_label(block: dict) -> str:
    name = block.get("name") or ""
    inp = block.get("input") or {}
    if name == "Bash":
        cmd = str(inp.get("command") or "")
        m = re.search(r"tool_cli\s+(\w+)", cmd)
        if m:
            return f"SKSQ 도구 호출: {m.group(1)}"
        return "명령 실행 중…"
    if name == "Read":
        return f"파일 읽는 중: {inp.get('file_path', '')}"
    if name == "Grep":
        return f"원문에서 검색: {inp.get('pattern', '')}"
    if name == "WebSearch":
        return f"웹 검색: {inp.get('query', '')}"
    return f"{name} 실행…"


def _answer_anthropic(question: str, history: list[dict] | None, max_rounds: int,
                      model: str | None = None, effort: str | None = None) -> Iterator[dict]:
    claude_exe = _resolve_claude_exe()
    if not claude_exe:
        yield {"type": "error",
               "text": "claude CLI 를 찾지 못했습니다. `npm install -g @anthropic-ai/claude-code` "
                       "설치 후 `claude login` 으로 로그인하세요."}
        return

    args = [
        claude_exe, "-p", _cli_prompt(question, history),
        "--append-system-prompt", _cli_system_prompt(),
        "--permission-mode", "bypassPermissions",
        "--add-dir", str(config.ROOT),
        "--output-format", "stream-json", "--verbose",
    ]
    if model:
        args += ["--model", model]

    try:
        proc = subprocess.Popen(
            args, cwd=str(config.ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=_claude_env(effort),
        )
    except OSError as e:
        yield {"type": "error", "text": f"claude CLI 실행 실패: {e}"}
        return

    final_text = ""
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        yield {"type": "progress", "text": _cli_tool_label(c)}
                    elif c.get("type") == "text" and c.get("text"):
                        final_text += c["text"]
                        yield {"type": "assistant_text", "text": c["text"]}
            elif ev.get("type") == "result":
                if ev.get("result") and not final_text:
                    final_text = ev["result"]
    finally:
        proc.wait()
        stderr_out = proc.stderr.read() if proc.stderr else ""

    if proc.returncode != 0 and not final_text:
        yield {"type": "error",
               "text": f"claude CLI 오류(code {proc.returncode}): {stderr_out[:300] or '(자세한 정보 없음)'}"}
        return
    yield {"type": "final", "text": final_text or "(claude CLI 로부터 응답을 받지 못했습니다)"}


# ── OpenAI (GPT) ──────────────────────────────────────────────────
def _answer_openai(question: str, history: list[dict] | None, max_rounds: int,
                   model: str | None = None, effort: str | None = None) -> Iterator[dict]:
    """/v1/responses 사용 (chat.completions 아님) — GPT-5.6 계열(Terra 등)은
    reasoning(기본 medium)과 function tools 를 chat.completions 에서 동시에 못 쓴다.
    /v1/responses 는 이 조합을 온전히 지원해서 reasoning_effort='none' 으로 낮출 필요가 없다."""
    import openai

    key = config.Keys.OPENAI
    if not key:
        yield {"type": "error", "text": "OPENAI_API_KEY 가 설정되지 않았습니다. .env 에 넣어주세요."}
        return
    model = model or config.OPENAI_MODEL
    client = openai.OpenAI(api_key=key)
    rkw = _openai_reasoning_kwargs(model, effort)

    tools = [
        {"type": "function", "name": t["name"], "description": t["description"],
         "parameters": t["input_schema"]}
        for t in registry.tool_schemas()
    ]
    input_list: list = [{"role": h["role"], "content": h["content"]} for h in (history or [])]
    input_list.append({"role": "user", "content": question})

    for _ in range(max_rounds):
        try:
            resp = client.responses.create(
                model=model, instructions=_system_prompt(), input=input_list, tools=tools,
                **rkw,
            )
        except openai.APIStatusError as e:
            # 추론 인자를 안 받는 모델(커스텀 ID 등)이면 인자를 떼고 한 번 더 시도한다.
            if rkw and _is_thinking_error(e):
                rkw = {}
                yield {"type": "progress",
                       "text": f"이 모델({model})은 추론강도 인자를 받지 않아 기본값으로 진행합니다"}
                try:
                    resp = client.responses.create(
                        model=model, instructions=_system_prompt(), input=input_list, tools=tools,
                    )
                except openai.APIStatusError as e2:
                    yield {"type": "error",
                           "text": f"OpenAI API 오류 {e2.status_code}: {e2.message}"}
                    return
            else:
                yield {"type": "error", "text": f"OpenAI API 오류 {e.status_code}: {e.message}"}
                return
        except openai.APIConnectionError as e:
            yield {"type": "error", "text": f"네트워크 오류: {e}"}
            return

        input_list += resp.output

        text = getattr(resp, "output_text", None) or ""
        if text:
            yield {"type": "assistant_text", "text": text}

        calls = [item for item in resp.output if getattr(item, "type", None) == "function_call"]
        if not calls:
            yield {"type": "final", "text": text}
            return

        for item in calls:
            try:
                args = json.loads(item.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool_use", "name": item.name, "input": args}
            result = registry.dispatch(item.name, args)
            yield {"type": "tool_result", "name": item.name, "input": args, "result": result}
            content = (json.dumps(result["value"], ensure_ascii=False)
                      if result["ok"] else result["error"])
            input_list.append({
                "type": "function_call_output", "call_id": item.call_id, "output": content,
            })

    # 상한 도달 — 에러만 내면 그동안 조회한 데이터가 전부 버려진다. tool_choice="none" 으로
    # 도구를 막고 한 번 더 불러 "지금까지 확보한 근거" 로 답을 만들게 한다.
    yield {"type": "progress", "text": f"라운드 상한({max_rounds}) 도달 — 확보한 근거로 정리 중"}
    try:
        resp = client.responses.create(
            model=model, instructions=_system_prompt(), input=input_list,
            tools=tools, tool_choice="none", **rkw,
        )
        text = getattr(resp, "output_text", None) or ""
    except Exception as e:  # noqa: BLE001
        yield {"type": "error",
               "text": f"라운드 상한({max_rounds}회)에 도달했고 마무리 응답도 실패했습니다: {e}"}
        return
    note = _round_limit_note(max_rounds)
    yield {"type": "final", "text": f"{note}\n\n{text}".strip() if text else note}
