# 다른 PC에서 이어서 작업하기

터미널 명령을 외울 필요 없다. **한 번만 명령어를 치고**, 그 뒤로는 전부 더블클릭이다.

---

## 새 PC에서 처음 한 번 (딱 3단계)

### 1단계 — Git 설치

시작 메뉴에서 **PowerShell** 을 열고 아래 한 줄을 붙여넣고 Enter:

```
winget install -e --id Git.Git ; winget install -e --id Python.Python.3.12
```

설치가 끝나면 **PowerShell 창을 닫았다가 다시 연다.** (설치된 프로그램을 인식시키려면 필요하다.)

> winget 이 안 되면 각각 직접 받아도 된다:
> [Git](https://git-scm.com/download/win) · [Python 3.12](https://www.python.org/downloads/release/python-3120/)
> (Python 설치 화면에서 **"Add python.exe to PATH"** 체크를 꼭 켤 것)

### 2단계 — 코드 받아오기

다시 연 PowerShell 에 아래를 붙여넣고 Enter. (바탕화면에 폴더가 생긴다)

```
cd $HOME\Desktop ; git clone https://github.com/aen6602-pixel/the-associate.git
```

GitHub 로그인 창이 뜨면 브라우저로 로그인하면 된다.

### 3단계 — 세팅 실행

생긴 `the-associate` 폴더를 열고 **`새PC_세팅.bat` 을 더블클릭.**

가상환경 만들기 · 패키지 설치 · `.env` 만들기 · API 키 입력까지 알아서 물어보며 진행한다.
**여기까지가 끝이다. 이 뒤로 명령어를 칠 일은 없다.**

---

## API 키는 어디서 가져오나

세팅 스크립트가 키를 하나씩 물어본다. 값은 **Railway 에서 그대로 복사**하는 게 제일 빠르다:

> Railway → 서비스 → **Variables** → *Raw Editor* → 거기 있는 값들을 그대로 붙여넣기

Railway 에 있는 것: `DART_API_KEY` `ECOS_API_KEY` `FRED_API_KEY` `EDINET_API_KEY`
`FINMIND_TOKEN` `OPENFIGI_API_KEY` `DEEPSEEK_API_KEY` `SEC_USER_AGENT`

Railway 에 **없어서 새로 발급**해야 하는 것 (없어도 앱은 돌아간다):
`OPENAI_API_KEY` `ANTHROPIC_API_KEY` `GEMINI_API_KEY` `TG_API_ID` `TG_API_HASH`

모르는 키는 **그냥 Enter** 로 건너뛰면 된다. 나중에 `새PC_세팅.bat` 을 다시 돌리면
**이미 채워진 키는 건너뛰고 비어있는 것만** 다시 물어본다.

> `TG_SESSION_STRING` 은 텔레그램 **계정 접근 권한 그 자체**다. 절대 복사해 옮기지 말고,
> 새 PC 에서 `텔레그램 로그인.bat` 을 돌려 새로 발급받는다.

### Claude 두뇌를 쓸 거면 (선택)

Anthropic 은 API 키가 아니라 로그인된 Claude Code CLI 를 재사용한다. 쓰려면 PowerShell 에서:

```
npm install -g @anthropic-ai/claude-code ; claude login
```

---

## 매일 쓰는 법 — 더블클릭 4개

| 파일 | 언제 |
|---|---|
| **`최신코드_받기.bat`** | 작업 **시작할 때**. 다른 PC에서 한 작업을 가져온다 |
| **`SKSQ_실행.bat`** | 앱 켜기 → http://localhost:8501 |
| **`커밋하고_배포.bat`** | 작업 **끝낼 때**. 저장 + GitHub + Railway 자동 배포 |
| `텔레그램 로그인.bat` | Market Muse 쓸 때만 |

### 순서만 지키면 안 꼬인다

```
   작업 시작  →  최신코드_받기.bat
                      ↓
                 SKSQ_실행.bat  으로 개발
                      ↓
   PC 를 바꾸기 전  →  커밋하고_배포.bat      ← 이걸 빼먹으면 꼬인다
```

**한 PC에서 `커밋하고_배포.bat` 을 안 하고 다른 PC로 넘어가면 충돌한다.**
`최신코드_받기.bat` 은 그런 상황을 감지하면 **아무것도 안 하고 멈춘 뒤** 알려준다.
멈췄다는 메시지가 나오면 화면을 그대로 복사해서 물어보면 된다. 직접 손대지 말 것.

---

## git 이 옮겨주는 것 / 아닌 것

| | git | 비고 |
|---|---|---|
| 코드 전부 (`server/ core/ engines/ providers/ agent/ web/ skills/ tests/`) | ✅ | |
| 배포 설정 (`railway.json` `Procfile` `.python-version` CI) | ✅ | |
| `.env` (API 키) | ❌ | 위 안내대로 새 PC에서 입력 |
| `.venv/` | ❌ | `새PC_세팅.bat` 이 만든다 |
| `sessions/` `output/` `.cache/` `muse.db` | ❌ | 대화기록·캐시. 재생성되니 안 옮겨도 된다 |

**Railway 에는 아무 설정도 새로 할 게 없다.** 배포는 GitHub 저장소를 보고 있어서,
어느 PC에서 push 하든 똑같이 배포된다.

---

## 자주 겪는 것

**"python 을 찾을 수 없습니다"** — Python 설치할 때 *Add python.exe to PATH* 를 안 켠 것.
Python 을 다시 설치하면서 그 체크박스를 켜면 된다.

**로그인 창이 계속 뜬다 / push 가 거부된다** — GitHub 계정 인증 문제.
`git push` 가 브라우저 로그인을 띄우면 그걸로 로그인하면 된다.

**앱은 켜지는데 "키가 없다"고 한다** — `.env` 가 비었다. `새PC_세팅.bat` 을 다시 더블클릭하면
비어있는 키만 다시 물어본다.

**배포가 안 나갔다** — Railway 는 GitHub Actions 테스트가 초록불일 때만 배포한다
("Wait for CI" 설정). https://github.com/aen6602-pixel/the-associate/actions 에서 빨간불이면
그 로그를 복사해서 물어보면 된다.
