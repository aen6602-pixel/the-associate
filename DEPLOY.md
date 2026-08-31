# 배포 가이드 — The Associate (Railway)

지정 소수 사용자에게 **로그인 게이트 + 자동 배포(CI/CD)** 로 서비스한다.
스택은 **FastAPI + 정적 웹 UI**, 호스트는 **Railway(Nixpacks 빌더)**.
Docker 도, Streamlit 도 쓰지 않는다.

---

## 아키텍처 (한 장 요약)

```
브라우저 (web/index.html + app.js + styles.css — 프론트엔드 빌드 스텝 없음)
   │  fetch(JSON) · SSE(text/event-stream)
   ▼
FastAPI  server/main.py
   ├── 로그인      core/auth.py       서명 쿠키(HttpOnly), APP_USERS 검증
   ├── 대화 영속화  core/history.py    DATA_DIR/sessions/<사용자>/*.json
   ├── 마크다운     core/markdown.py   답변 → HTML (HTML 리포트와 공용)
   └── 두뇌 중계    agent/brain.py     tool-use 이벤트를 SSE 로 그대로 흘림
                        ▼
              engines(계산) · providers(원본 추출)     ← 숫자는 여기서만 나온다
```

배포용으로 준비된 파일:

| 파일 | 역할 |
|---|---|
| [railway.json](railway.json) | 빌더 NIXPACKS, 시작 명령, 헬스체크 `/healthz` |
| [Procfile](Procfile) | 같은 시작 명령(Nixpacks 가 자동 감지하는 표준 경로) |
| [.python-version](.python-version) | 파이썬 3.12 고정 (Nixpacks·CI 공용) |
| [requirements.txt](requirements.txt) | 런타임 의존성 (버전 고정) |
| [core/auth.py](core/auth.py) | 로그인 게이트 — 미설정 + `DEPLOY_MODE=1` 이면 **앱을 열지 않음** |
| [core/paths.py](core/paths.py) | `DATA_DIR` 볼륨에 대화기록·캐시 저장(재배포에도 유지) |
| [.github/workflows/ci.yml](.github/workflows/ci.yml) | 테스트 + **Railway 와 같은 명령으로 실제 서버 부팅** 검증 |
| [tests/](tests/) | 로그인 게이트·세션 격리·쿠키 위조·스트리밍·마크다운 회귀 27건 |

---

## 사람이 해야 하는 일

Railway 에 GitHub 저장소를 이미 붙이고 브랜치를 `main` 으로 잡아둔 상태를 전제로 한다.

### 1. Variables (필수)

Railway → 서비스 → **Variables** → *Raw Editor* 에 붙여넣고 값 채우기.

```env
DEPLOY_MODE=1          # 사실 Railway 에선 자동 감지되지만, 명시해 두는 편이 안전하다
LLM_PROVIDER=deepseek
DATA_DIR=/data

# 로그인 — 이걸 안 넣으면 앱이 열리지 않는다(의도된 fail-closed)
APP_USERS=sanghwa:긴비밀번호1,동료이름:긴비밀번호2
# /admin 접근 계정. 생략하면 APP_USERS 의 첫 계정이 관리자
ADMIN_USERS=sanghwa
# 쿠키 서명 키 — 없으면 재시작마다 전원 로그아웃
SESSION_SECRET=<python -c "import secrets; print(secrets.token_hex(32))" 결과>

DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-v4-flash

DART_API_KEY=...
ECOS_API_KEY=...
FRED_API_KEY=...
EDINET_API_KEY=...
FINMIND_TOKEN=...
OPENFIGI_API_KEY=...
SEC_USER_AGENT=the-associate sanghwalee@sksquare.com
```

> `ANTHROPIC_MODEL` 은 넣지 않는다 — Claude 두뇌는 로컬 CLI 방식이라 서버에선 못 쓰고,
> `DEPLOY_MODE=1` 이 UI 목록에서 자동으로 숨긴다. `PORT` 도 넣지 않는다(Railway 가 주입).

### 2. Volume (권장)

서비스 → **Add Volume** → Mount path `/data`.
대화기록(`/data/sessions`)과 HTTP 캐시(`/data/.cache`)가 재배포에도 살아남는다.
없으면 사이드바에 `⚠︎ Ephemeral storage` 경고가 뜨고 재배포 때 기록이 사라진다.

### 3. Domain

Settings → Networking → **Generate Domain** → `*.up.railway.app` URL 발급.
URL 은 사용자에게, 비밀번호는 **다른 경로**로 전달.

### 4. CI/CD 마감 — "Wait for CI" 켜기

Railway 는 `main` push 를 감지해 자동 재배포한다. 여기에 안전장치를 얹는다:

- Settings → **"Wait for CI" 토글 ON**

이러면 GitHub Actions([ci.yml](.github/workflows/ci.yml))가 초록불일 때만 배포가 나간다.
CI 가 보는 것:

1. **tests** — 소스 전체 컴파일 + pytest 27건
   (미인증 차단, 오답 비밀번호 거절, 쿠키 위조·만료, 계정 삭제 후 쿠키 무효화,
   사용자간 세션 격리, 경로 조작, SSE 이벤트 순서, 두뇌 예외 복구, 마크다운/XSS)
2. **boot** — `uvicorn server.main:app …` 로 **Railway 와 같은 명령**으로 실제 서버를 띄워
   `/healthz` · 정적 UI · **미인증 401** · 로그인 후 접근까지 HTTP 로 확인

이후 배포 절차는 **`git push origin main` 하나**다.

---

## 배포 후 실측 체크리스트

- [ ] URL 접속 → **로그인 화면이 먼저 뜨는가** (안 뜨면 `APP_USERS` 누락)
- [ ] 틀린 비밀번호 → 거부되는가
- [ ] 로그인 후 "한국의 market risk premium은?" → 트레이스에 Damodaran 출처가 붙는가
- [ ] "삼성전자 매출액" → DART 응답이 오는가
- [ ] **네이버 시가총액**(comps 질문) — Railway 기본 리전은 US-West 다. 한국 소스가 막히면
      Settings → Region 을 **Southeast Asia (Singapore)** 로 바꿔 재시도
- [ ] 브라우저 **새로고침** → 로그인이 유지되는가(쿠키)
- [ ] 다른 계정으로 로그인 → **대화 목록이 서로 안 보이는지**
- [ ] HTML 리포트 · 전체 DCF 엑셀 다운로드
- [ ] 재배포 후 대화기록이 남아있는지(볼륨 확인)

## 비용

Railway 는 무료 티어가 없다(Hobby $5/월, 사용량 $5 포함). 상시 1 인스턴스 + 소량 트래픽이라
통상 포함분 안에서 끝난다. Settings → **Serverless(App Sleeping)** 를 켜면 무접속 시 잠들어
더 저렴해진다(첫 접속이 몇 초 느려짐).

---

## 관리자 페이지 (`/admin`)

`ADMIN_USERS` 에 지정된 계정만 볼 수 있다(미지정이면 `APP_USERS` 첫 계정). 로그인 후 사이드바의
**Admin** 버튼 또는 `https://<앱주소>/admin`.

보이는 것: 계정별 대화·질문·도구호출·실패 건수와 마지막 활동, 최근 질문 타임라인, 데이터 소스
사용 순위, 그리고 **대화 클릭 시 질문·답변·사용 소스 전문**.

권한은 화면이 아니라 API 에서 건다 — 비관리자는 `/api/admin/*` 이 403, 미로그인은 401 이다.

> 팀원의 질문과 답변 원문이 그대로 보인다. **조회 가능하다는 사실을 팀원에게 미리 알려두는 것을
> 권한다** — 관리자 화면 하단에도 같은 문구를 띄워 둔다.

## 알아둘 것

- **배포 모드는 자동 감지된다.** 배포 모드는 인증이 없으면 앱을 열지 않는(fail-closed) 스위치라,
  깜빡하면 공개 URL 이 게이트 없이 열린다. 그래서 `DEPLOY_MODE` 가 없어도 호스팅 환경변수
  (`RAILWAY_*` 등)가 보이면 배포로 간주한다([core/config.py](core/config.py) `detect_deploy_mode`).
  일부러 끄려면 `DEPLOY_MODE=0` 을 명시해야 한다.
- **`/healthz` 는 앱이 실제로 살아있음을 뜻한다.** 이 응답은 FastAPI 라우팅까지 도달해야 나오므로,
  모듈 import 실패·설정 오류면 헬스체크가 실패해 Railway 가 이전 배포를 유지한다.
  (참고: 이전 Streamlit 구성의 `/_stcore/health` 는 앱 스크립트를 실행하지 않고도 200 을 줘서
  크래시를 못 잡았다 — 그래서 헬스체크를 앱 라우트로 옮겼다.)
- **로그인 유지**는 서명 쿠키(HttpOnly, SameSite=Lax, HTTPS 면 Secure)로 한다. 서버가 세션을
  메모리에 들고 있지 않으므로 재배포해도 `SESSION_SECRET` 이 같으면 로그인이 유지된다.
- **동시 사용자**: uvicorn 1 워커 + Starlette threadpool. `brain.answer()` 는 동기 제너레이터라
  스레드에서 돌아가므로 한 사람의 긴 조사가 다른 사람을 막지 않는다.
- Railway GitHub App 은 private 저장소도 읽는다 → Streamlit Cloud 때문에 Public 으로 열어둔
  저장소를 **Private 으로 되돌려도 된다.**

## 로컬 실행 (개발)

```powershell
# 게이트 없음(APP_USERS 미설정) · .env 로 키 로드 · 코드 수정 시 자동 재시작
.\.venv\Scripts\python -m uvicorn server.main:app --reload --port 8501
#  → http://localhost:8501

.\.venv\Scripts\python -m pytest -q      # 배포 회귀 테스트 27건
```

`SKSQ_실행.bat` 을 더블클릭해도 같은 서버가 뜨고 브라우저가 열린다.
