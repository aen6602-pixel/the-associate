# 배포 가이드 — The Associate

지정 소수 사용자에게 **로그인 게이트 + $0 호스팅**으로 배포하는 방법.

## 채택: Streamlit Community Cloud (무료 비공개 앱)

- 무료 티어에 **비공개 앱 1개** → 지정 이메일만 접근(Google 로그인/1회용 이메일 링크).
- 시크릿 매니저 제공(대시보드), GitHub 브랜치 연결 배포, push 시 자동 재배포.
- 제약: ~1GB RAM, 12h 무접속 시 슬립(첫 접속 시 깨어남), 커스텀 도메인 불가, 비공개 앱 1개.
- **저장소 휘발성**: 컨테이너 재시작 시 `sessions/` 대화기록은 사라짐(소규모 도구엔 감수).

### 배포 단계
1. `v2` 브랜치에 최신 변경을 push (본인 터미널에서 `git push`).
2. https://share.streamlit.io → GitHub 로그인 → **New app**
   - Repository: `aen6602-pixel/the-associate`
   - Branch: **`v2`**
   - Main file path: `app.py`
3. 앱을 **Private** 로 설정 → **뷰어 이메일 허용목록**에 지정 사용자 추가.
4. **Settings → Secrets** 에 아래 TOML 붙여넣기(값 채우기).
5. Deploy → 발급된 URL 을 지정 사용자에게 공유.

### 설정할 시크릿 (Settings → Secrets, TOML)
```toml
LLM_PROVIDER = "openai"
DEPLOY_MODE  = "1"            # claude(CLI) 두뇌 숨김

OPENAI_API_KEY = "sk-..."
OPENAI_MODEL   = "gpt-5.6-terra"   # 또는 원하는 모델 ID

DART_API_KEY    = "..."
ECOS_API_KEY    = "..."
FRED_API_KEY    = "..."
EDINET_API_KEY  = "..."
FINMIND_TOKEN   = "..."
OPENFIGI_API_KEY = "..."
SEC_USER_AGENT  = "the-associate you@sksquare.com"
# ANTHROPIC 은 로컬 CLI 방식이라 배포엔 불필요
```
> 코드는 `st.secrets` 를 자동으로 `os.environ` 으로 옮긴 뒤 config 를 로드한다([app.py](app.py) 상단 shim). `.env` 는 배포에 쓰지 않으며 절대 커밋 금지.

### 배포 후 필수 점검 (한국 데이터 도달성)
Streamlit Cloud 는 **US 리전**에서 실행된다. 로그인 후 아래를 실측:
- DART 재무항목 조회 (예: "삼성전자 매출액")
- **네이버 시가총액**(comps) — 해외 IP 차단 가능성. 실패하면 아래 폴백으로 전환.
- 두 번째 사용자로 로그인해 **대화가 서로 안 보이는지**(사용자별 격리) 확인.

## 폴백: Oracle Cloud Always Free VM (서울 리전)
Streamlit Cloud 에서 네이버/한국 소스가 막히거나 자원이 부족하면:
- **Oracle Always Free** ARM VM(ap-seoul-1, 2 OCPU/12GB, $0 상시가동)에 컨테이너/venv 로 `streamlit run`.
- 로그인 게이트: **Cloudflare Tunnel + Cloudflare Access**(무료 이메일 OTP) 로 앞단 보호.
- 서울 리전이라 DART/네이버 도달성 문제 해소. 대신 VM·리버스프록시 세팅 필요(난이도↑).

## 로컬 실행 (개발)
```powershell
.\.venv\Scripts\streamlit run app.py    # .env 로 키 로드, DEPLOY_MODE 미설정 → Claude(CLI) 두뇌도 보임
```
