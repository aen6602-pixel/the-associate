# Anthropic `financial-services` — 참고용 미러 (valuation 중심)

출처: https://github.com/anthropics/financial-services (branch `main`)
받은 날짜: 2026-08-07 · 원본 라이선스: Apache 2.0

이 폴더는 원본 레포 전체가 아니라 **SK Square 밸류에이션 업무에 바로 쓸 만한 파일만 골라 미러**한 것이다.
전체 트리(377개 파일)는 아래 "원본 레포 전체 구조" 참고. 나머지 파일이 필요하면 [원본 새로고침](#원본-새로고침) 절차로 추가 다운로드.

---

## 이 레포가 무엇인가

Anthropic이 만든 금융권(IB / equity research / PE / wealth) 워크플로우용 **레퍼런스 agent · skill · data connector 모음**.
하나의 소스가 두 가지 방식으로 동작한다:

1. **Claude Cowork / Claude Code 플러그인** (대화형)
2. **Claude Managed Agents API** (`/v1/agents`, headless)

> 중요 원칙 (원본 README·CLAUDE.md 명시): 모든 agent는 **사람이 검토할 초안(work product)만 생성**한다. 투자 의사결정·거래 실행·리스크 확정·원장 기표를 스스로 하지 않는다. — 우리 "할루시네이션 없는 밸류에이션 에이전트" 방향과 정확히 일치.

### skill의 구조 (우리가 배울 핵심 패턴)
- skill은 코드가 아니라 **`SKILL.md` (마크다운 지시문) + references/ + scripts/** 구성. 빌드 스텝 없음.
- 원천은 `vertical-plugins/`에 authoring → `sync-agent-skills.py`로 각 agent 번들에 복제 (single-source).
- 데이터는 **MCP 커넥터**로 붙임 (`.mcp.json`). Daloopa, Morningstar, S&P CapIQ(kfinance), FactSet, Moody's, PitchBook, LSEG 등.

---

## 미러한 파일 — 밸류에이션 관점 하이라이트

### 1. DCF (가장 중요, 49KB 상세)
- [`financial-analysis/skills/dcf-model/SKILL.md`](plugins/vertical-plugins/financial-analysis/skills/dcf-model/SKILL.md)
  - IB 스탠다드 DCF를 Excel로 산출하는 institutional-quality 워크플로우.
  - **배울 점 (우리 원칙과 직결):**
    - **Formulas over hardcodes (NON-NEGOTIABLE):** 모든 projection/margin/PV 셀은 살아있는 Excel 수식이어야 함. Python으로 계산해 숫자만 박으면 안 됨 → 가정 바꾸면 모델이 flex 되어야.
    - 허용된 하드코딩은 (1) 과거 실적 raw input (2) 가정 driver(성장률·WACC·terminal g) (3) 현재 시장데이터(주가·부채)뿐.
    - **모든 blue input 셀에 cell comment로 출처 명기** — "Source: [System/Doc], [Date], [Ref], [URL]". ← 할루시네이션 방지 = 출처 추적성.
    - **단계별로 유저와 확인하며 진행** (raw input → 매출 → FCF → WACC → equity bridge → 민감도). 끝까지 한 번에 만들지 말 것.
    - 민감도표는 홀수 x 홀수(5x5/7x7), center cell = base case, 중앙셀 출력 = 실제 implied 주가(정합성 체크).
- [`.../dcf-model/scripts/validate_dcf.py`](plugins/vertical-plugins/financial-analysis/skills/dcf-model/scripts/validate_dcf.py) — 완성 모델 자동 검증 스크립트.
- [`.../dcf-model/TROUBLESHOOTING.md`](plugins/vertical-plugins/financial-analysis/skills/dcf-model/TROUBLESHOOTING.md) · [`requirements.txt`](plugins/vertical-plugins/financial-analysis/skills/dcf-model/requirements.txt)

### 2. 상대가치 / 거래사례
- [`financial-analysis/skills/comps-analysis/SKILL.md`](plugins/vertical-plugins/financial-analysis/skills/comps-analysis/SKILL.md) (30KB) — Trading comps(EV/EBITDA, P/E 등) 스프레딩.
- [`financial-analysis/skills/lbo-model/SKILL.md`](plugins/vertical-plugins/financial-analysis/skills/lbo-model/SKILL.md) — LBO / IRR.

### 3. 3-statement 모델링 (밸류에이션의 기반)
- [`financial-analysis/skills/3-statement-model/SKILL.md`](plugins/vertical-plugins/financial-analysis/skills/3-statement-model/SKILL.md) (21KB) + references:
  [formulas.md](plugins/vertical-plugins/financial-analysis/skills/3-statement-model/references/formulas.md) ·
  [formatting.md](plugins/vertical-plugins/financial-analysis/skills/3-statement-model/references/formatting.md) ·
  [sec-filings.md](plugins/vertical-plugins/financial-analysis/skills/3-statement-model/references/sec-filings.md)

### 4. 밸류에이션 방법론 종합 문서 (읽기 좋은 개론)
- [`equity-research/.../initiating-coverage/references/valuation-methodologies.md`](plugins/vertical-plugins/equity-research/skills/initiating-coverage/references/valuation-methodologies.md)
  — DCF / Trading Comps / Precedent Transactions 3대 방법론 + **valuation reconciliation**(방법론 간 결과 조율) 단계별 가이드. UFCF 공식, WACC, terminal value 포함.
- [`.../initiating-coverage/SKILL.md`](plugins/vertical-plugins/equity-research/skills/initiating-coverage/SKILL.md) (30KB) — 커버리지 개시 리포트 전체 워크플로우(company research → modeling → valuation → chart → assembly).
- [`.../references/task2-financial-modeling.md`](plugins/vertical-plugins/equity-research/skills/initiating-coverage/references/task2-financial-modeling.md) ·
  [`.../references/task3-valuation.md`](plugins/vertical-plugins/equity-research/skills/initiating-coverage/references/task3-valuation.md)

### 5. PE / NAV / 밸류에이션 리뷰 (SK Square는 지주형 → NAV/SOTP 성격에 특히 유관)
- [`private-equity/skills/returns-analysis/SKILL.md`](plugins/vertical-plugins/private-equity/skills/returns-analysis/SKILL.md) — IRR/MOIC 등 수익률 분석.
- [`private-equity/skills/portfolio-monitoring/SKILL.md`](plugins/vertical-plugins/private-equity/skills/portfolio-monitoring/SKILL.md) — 포트폴리오사 모니터링.
- [`private-equity/skills/ic-memo/SKILL.md`](plugins/vertical-plugins/private-equity/skills/ic-memo/SKILL.md) — IC 메모.
- [`agent-plugins/valuation-reviewer/agents/valuation-reviewer.md`](plugins/agent-plugins/valuation-reviewer/agents/valuation-reviewer.md) — **Valuation Reviewer agent의 시스템 프롬프트** (GP 패키지 → valuation 템플릿 → LP 리포팅). 우리 에이전트 프롬프트 설계에 참고.
- [`managed-agent-cookbooks/valuation-reviewer/agent.yaml`](managed-agent-cookbooks/valuation-reviewer/agent.yaml) + [README](managed-agent-cookbooks/valuation-reviewer/README.md) — headless(Managed Agent) 배포 형태 + subagent(package-reader / valuation-runner / publisher) 분리 예시.

### 6. 데이터 무결성 / 감사
- [`financial-analysis/skills/audit-xls/SKILL.md`](plugins/vertical-plugins/financial-analysis/skills/audit-xls/SKILL.md) — Excel 모델 감사(수식·링크·오류 점검). 할루시네이션/실수 방지 체크에 유용.

### 7. 최상위 문서
- [README.md](README.md) — 전체 개요·설치·커넥터.
- [CLAUDE.md](CLAUDE.md) — 레포 구조·개발 규칙(single-source skill, `check.py`, `.ps1` ASCII 규칙 등).

---

## SK Square 밸류에이션 에이전트에 적용할 시사점 (요약)

1. **skill = 마크다운 지시문**이라는 구조를 그대로 채택 가능. 코드 없이 `SKILL.md`로 방법론·검증규칙을 문서화하면 우리 에이전트도 동일 패턴으로 확장.
2. **출처 주석 강제(cell comment: Source/Date/Ref/URL)** = "공개 데이터 기반 · 할루시네이션 없음"을 구조적으로 담보하는 방법. 우리 원칙과 1:1 대응 → 그대로 도입 권장.
3. **Formulas-over-hardcodes** + **단계별 유저 확인** 패턴은 검증 가능한(재현 가능한) 밸류에이션에 직접 적용.
4. **valuation-reviewer**의 subagent 분리(reader / runner / publisher)와 "초안만 생성, 사람 검토" 원칙은 우리 에이전트 아키텍처 레퍼런스.
5. 데이터 소싱은 MCP 커넥터로 추상화 — 우리는 DART(이미 skill 보유) 등 한국 공개데이터 소스를 같은 방식으로 붙이면 됨.

---

## 원본 레포 전체 구조 (다운로드 안 한 부분 포함)

```
financial-services/
├── plugins/
│   ├── agent-plugins/        # 완성형 named agent 10종 (각자 skill 번들 포함)
│   │   ├── earnings-reviewer / gl-reconciler / kyc-screener / market-researcher
│   │   ├── meeting-prep-agent / model-builder / month-end-closer / pitch-agent
│   │   └── statement-auditor / valuation-reviewer
│   ├── vertical-plugins/     # skill 원천 + commands + MCP
│   │   ├── financial-analysis  (core: comps, dcf, lbo, 3-statement, audit-xls, clean-data-xls,
│   │   │                        competitive-analysis, ib-check-deck, pptx-author, skill-creator ...)
│   │   ├── equity-research     (initiating-coverage, earnings-analysis, morning-note, thesis-tracker,
│   │   │                        catalyst-calendar, model-update, sector-overview, idea-generation)
│   │   ├── investment-banking  (cim-builder, teaser, merger-model, buyer-list, process-letter,
│   │   │                        pitch-deck, datapack-builder, deal-tracker, strip-profile)
│   │   ├── private-equity      (deal-sourcing, deal-screening, dd-checklist, dd-meeting-prep,
│   │   │                        ic-memo, returns-analysis, portfolio-monitoring, unit-economics,
│   │   │                        value-creation-plan, ai-readiness)
│   │   ├── wealth-management    (client-review, financial-plan, portfolio-rebalance, tax-loss-harvesting ...)
│   │   ├── fund-admin           (gl-recon, nav-tieout, accrual-schedule, roll-forward, break-trace,
│   │   │                        variance-commentary)
│   │   └── operations           (kyc-doc-parse, kyc-rules)
│   └── partner-built/
│       ├── lseg      (bond RV, FX carry, swap curve, option vol, macro rates, equity-research ...)
│       └── spglobal  (tear-sheet, earnings-preview-beta, funding-digest — CapIQ 기반)
├── managed-agent-cookbooks/  # 각 named agent의 headless(CMA) 배포 템플릿 (agent.yaml + subagents/)
├── claude-for-msft-365-install/  # Excel/PPT/Word/Outlook 애드인 어드민 프로비저닝
└── scripts/  # deploy-managed-agent.sh, orchestrate.py, check.py, validate.py, sync-agent-skills.py
```

### 주요 MCP 데이터 커넥터 (원본 `.mcp.json`)
Daloopa · Morningstar · S&P Global(kfinance/Kensho) · FactSet · Moody's · MT Newswires ·
Aiera · LSEG · PitchBook · Chronograph · Egnyte · Box

---

## 원본 새로고침

`gh` 미설치 + `raw.githubusercontent.com` 403 환경이라 **GitHub Contents API(base64)** 로 받았다.
전체 트리 재확인:
```powershell
(Invoke-RestMethod "https://api.github.com/repos/anthropics/financial-services/git/trees/main?recursive=1" `
  -Headers @{ "User-Agent"="claude" }).tree | Where-Object type -eq blob | Select-Object -Expand path
```
개별 파일 받기 (`<PATH>`를 위 트리의 경로로):
```powershell
$r = Invoke-RestMethod "https://api.github.com/repos/anthropics/financial-services/contents/<PATH>?ref=main" `
  -Headers @{ "User-Agent"="claude" }
[IO.File]::WriteAllBytes("<LOCAL>", [Convert]::FromBase64String($r.content))
```
> 참고: 미인증 API는 시간당 60회 rate limit. 대량 다운로드 시 토큰 헤더(`Authorization: Bearer ...`) 추가.
