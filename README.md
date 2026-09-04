# The Associate

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
[UI]   web/          ── 정적 HTML/CSS/JS (빌드 스텝 없음). 입력창 + 답변 + 트레이스
[HTTP] server/main.py ── FastAPI. 로그인·세션·SSE 스트리밍·산출물 내보내기
[두뇌]  agent/brain.py ── LLM tool-use: 어떤 도구를, 어떤 인자로 부를지만 결정
[격리]  agent/muse.py  ── /muse 전용. 텔레그램 채널 글만 근거로 한 단발 호출 (도구 없음)
──────────────────────────────────────────────────────────
[engines]   wacc / comps / 상증법 / dcf          ── 계산 (결정론 코드)
            business_mix                       ── 적용 판단(단일 DCF / SOTP / 지분평가)
            market_data                        ── 시장 중립 계층(KR·US·JP·TW 공통)
[providers] dart / ecos / fred / naver / damodaran / sec / edinet / finmind / fx
[core]      schema(=Provenance) / config / auth / history / markdown / http
```

- 모든 수치는 `core.schema.Value` 로 감싸 **출처(Provenance)** 를 달고 다닌다.
- 데이터가 없으면 `DataError` 를 올린다 (조용히 0/None 반환 금지).

## 데이터 소스 (한/미/일/대만 + 글로벌)

| 축 | 🇰🇷 | 🇺🇸 | 🇯🇵 | 🇹🇼 | 글로벌 |
|---|---|---|---|---|---|
| 공시·재무 | DART | SEC EDGAR | EDINET | FinMind / MOPS | — |
| 주가·시총 | 네이버 금융 | yfinance | yfinance | FinMind | — |
| 무위험이자율 | ECOS | FRED | 財務省/FRED | CBC/FRED | — |
| ERP·산업베타·세율 | — | — | — | — | **Damodaran** |
| 매크로 | ECOS/KOSIS | FRED | BOJ | DGBAS | — |
| 환율 | — | — | — | — | frankfurter(ECB) |
| M&A·거래사례 | DART 평가의견서 | EDGAR DEFM14A | EDINET TOB | MOPS | — |

## 실행

```powershell
# 1) 키 설정
copy .env.example .env   # 값 채우기

# 2) 실행  →  http://localhost:8501
.\.venv\Scripts\python -m uvicorn server.main:app --reload --port 8501

# 3) 테스트 (로그인 게이트 · 세션 격리 · 스트리밍 · 마크다운)
.\.venv\Scripts\python -m pytest -q
```

`SKSQ_실행.bat` 을 더블클릭해도 같은 서버가 뜨고 브라우저가 열린다.

### 다른 PC에서 이어서 작업하려면

터미널을 쓸 필요 없다. [새PC_시작하기.md](새PC_시작하기.md) 를 그대로 따라가면 된다.

| 더블클릭 | 언제 |
|---|---|
| `새PC_세팅.bat` | 새 PC에서 처음 한 번 — venv·패키지·`.env` 키까지 알아서 |
| `최신코드_받기.bat` | 작업 시작할 때 (다른 PC에서 한 작업 가져오기) |
| `커밋하고_배포.bat` | 작업 끝낼 때 (저장 + CI + Railway 자동 배포) |

## Market Muse (`/muse`) — 구독 텔레그램 채널 브리핑

구독 중인 채널 40여 개의 최근 글을 직접 읽어 두고, 거기서만 찾아 답한다.

> ⚠️ **공신력이 없는 소스다.** 공시가 아니고, 정정·삭제 이력이 없으며, 작성자를 검증할 수
> 없다. 그래서 별도 화면(`/muse`)에만 살고 **밸류에이션 도구(`agent/registry.py`)에는
> 등록하지 않는다** — 이 경계는 [tests/test_marketmuse.py](tests/test_marketmuse.py) 가
> 지킨다. 여기서 본 수치를 평가에 쓰려면 사람이 공시로 확인한 뒤 가정으로 옮겨 적어야 한다.

| 쓰는 법 | |
|---|---|
| 그냥 묻기 | "HBM 관련 최근 언급" — 관련 글을 찾아 채널명·날짜와 함께 정리 |
| 채널 지목 | "**잠실개미** 채널에서 반도체 뭐래?" — 별칭을 알아듣고 그 채널로 좁힌다 |
| 후속 질문 | "그거 왜 그래?" — 직전 질문을 검색어에 얹어 다시 찾는다 |
| 최근 흐름 브리핑 | 물어볼 게 아직 없을 때. 최근 글을 주제별로 묶어 준다 |
| 채널 관리 | 사이드바에서 추가·삭제 (**관리자만**). `@아이디`·t.me 링크·숫자 id 다 받는다 |

채널 목록은 `muse_channels.txt` 가 **씨앗**이고, 첫 실행 때 `DATA_DIR` 로 복사된 뒤로는
볼륨 사본이 진짜다 — 화면에서 더하고 뺀 것이 재배포에도 남는다. `#` 뒤는 별칭이다.

### 한 번만 하는 준비 (로컬)

채널 글은 **봇 토큰으로 못 읽는다**(봇은 자신이 속한 대화만 본다). 사람 계정 세션이
필요하고, 그 로그인은 전화로 오는 코드를 넣어야 해서 서버에서 자동화할 수 없다.

1. https://my.telegram.org → API development tools → `api_id`/`api_hash` 를 `.env` 에
2. `텔레그램 로그인.bat` 더블클릭 → 인증코드 입력 → `TG_SESSION_STRING` 이 `.env` 에 자동으로 채워진다
3. 배포에도 쓰려면 그 값을 Railway 환경변수에 그대로 넣는다

> `TG_SESSION_STRING` 은 **계정 접근 권한 그 자체다.** 채팅·메일·이슈에 붙여넣지 말 것.

## 작업 절차서 (skills)

`skills/<이름>/SKILL.md` 하나가 절차서 하나다. 정식 가치평가처럼 **절차와 승인이 중요한 작업**을
요청받으면 두뇌가 `load_skill` 로 읽고 그대로 따른다.

```
skills/valuation-agent/
  SKILL.md                 # frontmatter(name·description) + 절차
  references/*.md          # 방법별 세부 규칙 (필요한 것만 read_skill_reference 로 읽음)
```

시스템 프롬프트에는 **이름·설명만** 들어가고 본문은 호출 시점에만 로드된다(progressive
disclosure) — 전부 상주시키면 매 요청이 무거워지고 tool-calling 정확도가 떨어진다.

**추가하려면**: `skills/` 밑에 폴더를 만들고 `SKILL.md` 를 두면 끝이다. 코드 수정은 없다.

## 배포

Railway(Nixpacks) + GitHub 자동배포. `main` 에 push 하면 CI 통과 후 자동으로 올라간다.
환경변수 목록과 절차는 [DEPLOY.md](DEPLOY.md).

배포 앱은 **로그인 게이트**([core/auth.py](core/auth.py))를 지난 사용자만 쓸 수 있고,
대화기록은 사용자별로 격리되어 볼륨(`DATA_DIR`)에 저장된다.
