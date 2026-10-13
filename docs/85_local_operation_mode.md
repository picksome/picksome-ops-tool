# 85. 로컬 운영 모드 — 아이콘 하나로 시작·종료·따라잡기

2026-08-17. 선행: `34_local_dev_setup_no_docker.md`, `49_worker_operations_runbook.md`,
`70_backup_and_key_recovery.md`, `84_autopilot_phase1_audit.md`.

Seller OS 는 운영 가능한 단계까지 왔지만 **상시 서버가 없다.** 당분간은 개인
Windows PC 에서 돌린다. 그래서 사용자가 매번 터미널 네 개를 열어 DB·백엔드·
프론트·워커를 손으로 띄우고 있었다.

이번 작업의 범위는 그것 하나다.

```
PC 를 켠다 → 아이콘 한 번 → 화면이 열리고 데이터가 최신이 된다
```

**새 업무 기능을 만들지 않았다.** 수집·정산·마감은 손대지 않았다.

---

## 1. 무엇이 실제로 도는가 (조사 결과)

문서가 아니라 저장소와 실행 중인 PC 를 봤다.

| 구성요소 | 실제 실행 방식 |
|---|---|
| PostgreSQL | 네이티브 설치 `C:\PostgreSQL\16` · 서비스 `postgresql-16` · 데이터 `C:\Program Files\PostgreSQL\16\data` |
| 백엔드 | `cd backend && uvicorn app.main:app --port 8000` · 루트 `.env` 를 절대경로로 읽는다 |
| 프론트엔드 | `next build` → `output: "standalone"` → `node .next/standalone/server.js` (Dockerfile 과 같은 규약) |
| 워커 | `python -m app.scripts.run_workers --once --trigger SCHEDULED` — **일회성**이다. 주기는 바깥이 준다 |
| 스케줄러 | 백엔드 프로세스 안 APScheduler 는 **알림 5종 전용**. 워커 주기는 외부(cron/systemd)가 담당 |
| 백업 | `python -m scripts.run_backup` — 외부 스케줄러가 부른다. 앱 안에서 돌지 않는다 |

### 1-1. 실측에서 배운 것 — 서비스 상태를 믿으면 안 된다

```
Get-Service postgresql-16   → Stopped
sc.exe query postgresql-16  → STOPPED
실제 5432 포트              → postgres.exe 7개가 붙어 있고 질의도 된다
```

서비스 상태만 봤으면 **멀쩡히 돌고 있는 DB 를 다시 띄우려** 했을 것이다.
그래서 기동 스크립트의 DB 판정 기준은 **실제 질의 성공**이다. 프로세스가
있다는 사실도, 포트가 열렸다는 사실도 근거로 쓰지 않는다.

---

## 2. 진짜 문제는 스크립트가 아니었다

터미널을 대신 열어 주는 것은 쉽다. 어려운 것은 **PC 를 꺼 둔 동안**이다.

`order_sync.compute_window` 는 이랬다.

```python
window_start = max(초기 시작일, now - overlap_days)
```

겹쳐 읽기가 3일인데 PC 를 18일 꺼 두면, 다시 켰을 때 창은 여전히
**최근 3일**이다. 그 사이 결제된 주문은 **영원히 읽히지 않는다.**
실패로 기록되지도 않는다 — 조용히 없는 매출이 된다.

서버에서는 이 구멍이 잘 안 보인다. 24시간 도니까 주기가 밀릴 일이 없다.
개인 PC 로 내려오는 순간 기본 동작이 된다.

### 2-1. 고친 방법 — 창을 **넓히기만** 한다

```python
anchor       = min(now, 마지막 성공)      # 없으면 now
window_start = max(초기 시작일, anchor - overlap_days)
```

`min` 이 핵심이다.

| 상황 | 창 |
|---|---|
| 마지막 성공이 방금 | `now - overlap` (예전과 같다) |
| 마지막 성공이 18일 전 | `18일 + overlap` 전부 |
| 마지막 성공 기록 없음 | `now - overlap` (예전과 같다) |
| 마지막 성공이 **미래** (시계 틀어짐) | `now - overlap` — `min` 이 막는다 |

**창이 짧아지는 방향으로는 절대 움직이지 않는다.** 이게 중요한 이유:
NEXT ENGINE 에는 수정일이 없어서(docs/73) "지난번 이후" 만 읽으면 이미 받은
주문의 변경을 영원히 못 본다. 예전 결정("`last_successful_sync_at` 은 조회
범위를 정하지 않는다")이 지키려던 것이 그것이고, 그건 그대로 살아 있다.

창 분할은 손대지 않았다 — 커넥터의 `split_windows` 가 벤더 한도(10일)로
자른다. 그래서 한 달을 못 돌았어도 **요청 하나가 10일을 넘지 않는다.**

클레임도 같다. 다만 마지막 성공을 다른 데서 읽는다:

```
주문   external_account.last_successful_sync_at   (주문 동기화가 소유)
클레임 external_sync_run 의 CLAIMS · SUCCESS 최댓값
```

`external_account.last_successful_sync_at` 을 클레임이 같이 쓰면 주문만
돌아도 클레임이 최신인 것처럼 보인다. **그 거짓이 정확히 M057 이 고친
문제다**(docs/84 §1).

### 2-2. 이 계산은 스케줄러를 모른다

절전·최대절전에서 깨어난 뒤 놓친 주기를 보정하는 장치를 만들지 않았다.
필요가 없다 — 같은 입력이면 얼마나 자주 돌았는지와 무관하게 같은 답이 나온다.
보정 장치를 두면 **그 장치가 또 틀린다.**

---

## 3. M058 — 실행 주체를 사실대로 적는다

`external_sync_run.trigger` CHECK 가 `MANUAL / SCHEDULED / WEBHOOK` 뿐이었다.
실제 주체는 넷이다.

```
MANUAL     사람이 화면에서 눌렀다
SCHEDULED  주기가 되어 돌았다            → 안 돌았으면 스케줄이 멈춘 것
API        관리자가 worker 를 직접 실행했다
STARTUP    PC 를 켜고 따라잡았다          → 안 돌았으면 PC 가 꺼져 있던 것
```

지금까지는 자동화 실행기가 `trigger="SCHEDULED"` 를 **못박아** 넘겨서 이
차이가 드러나지 않았다. 못을 뽑으니 CHECK 에 걸렸다 — 어휘를 `worker_run`
(M030 에서 이미 `API`/`STARTUP` 을 쓴다)에 맞췄다. 새 값을 지어낸 것이 아니다.

> 부수 효과로 기존 결함 하나가 드러났다. 관리 화면의 `POST
> /operations/workers/automation_scan/run` 은 `trigger="API"` 로 도는데,
> 못박힌 `SCHEDULED` 가 그걸 가리고 있었다. 못을 뽑기만 하고 CHECK 를
> 안 넓혔으면 그 버튼이 500 으로 죽었을 것이다.

되돌리기는 `STARTUP`/`API` 행이 남아 있으면 **거부한다.** 조용히 `SCHEDULED`
로 바꾸면 되돌리기가 "PC 가 꺼져 있었다" 를 "스케줄러가 돌았다" 로 위조하는
셈이다(M057 downgrade 와 같은 판단).

---

## 4. 중단된 동기화 기록 — 개인 PC 에서는 흔한 일

`worker_runtime.reclaim_stale_runs` 는 `worker_run` 만 정리했다.
**`external_sync_run` 은 아무도 안 치웠다.**

서버에서는 드문 일이다. 개인 PC 에서는 동기화 도중 전원을 끄면 매번 생긴다.
이번 작업 중에도 실제로 만들었다.

```
CLAIMS | STARTUP | RUNNING | 12:44:40 | (finished_at 없음)
```

advisory lock 은 연결이 끊기면 풀리므로 **다음 실행이 막히지는 않는다.**
문제는 기록이다 — 상태 화면이 계속 "지금 조회 중" 이라고 말하고, 그게
마지막 시도로 남는다.

`order_sync.reclaim_stale_sync_runs` 를 추가했다. 기동 따라잡기와
`run_workers --reclaim-only` 가 부른다. **읽은 건수를 추측하지 않는다** —
중단된 실행이 몇 건을 읽었는지는 알 수 없고, 0 으로 덮으면 "0건 읽고 성공"
처럼 보인다.

---

## 5. 파일

### 5-1. 플랫폼 중립 (파이썬 · 서버로 옮겨도 그대로)

| 파일 | 하는 일 | 종료 코드 |
|---|---|---|
| `backend/app/scripts/preflight.py` | 기동 전 환경 점검. **DB 에 붙지 않는다** | 0 통과 / 1 차단 |
| `backend/app/scripts/ops_status.py` | 상태 조회. **읽기 전용** | 0 정상 / 1 확인필요 / 2 DB 불가 |
| `backend/app/scripts/startup_catchup.py` | 못 돈 기간 따라잡기 | 0 최신 / 1 일부 실패 / 2 실행 불가 |

### 5-2. Windows 런처 (나중에 systemd/compose 로 교체될 부분)

| 파일 | 하는 일 |
|---|---|
| `scripts/start_seller_os.ps1` | 시작 10단계 |
| `scripts/stop_seller_os.ps1` | 종료 (기본적으로 DB 는 안 건드린다) |
| `scripts/status_seller_os.ps1` | 서비스 + 데이터 최신성 |
| `scripts/restart_seller_os.ps1` | stop → start 조합 |
| `scripts/run_worker_loop.ps1` | 워커 주기 루프 (cron 대역) |
| `scripts/install_local_shortcuts.ps1` | 바탕화면 바로가기 3개 |
| `scripts/install_autostart.ps1` | 로그인 자동 시작 (**기본 미등록**) |
| `scripts/lib/SellerOs.psm1` | 공용 함수 |
| `scripts/test/Test-LocalOperations.ps1` | 셸 계층 검사 15개 |

로그·실행기록은 `var/log/`, `var/run/` (둘 다 `.gitignore`).
로그 보존 14일.

---

## 6. 시작 순서와 그 이유

```
 1 환경 점검      DB 를 켜기 전에 본다. 접속 문자열이 비었는데 DB 부터 띄우면
                  "DB 가 안 뜬다" 로 보이고 사람이 엉뚱한 곳을 고친다
 2 DB            실제 질의로 판정. 떠 있으면 재사용, 없으면 서비스 시작 후 재확인
 3 스키마 리비전   AHEAD 면 여기서 중단
 4 백엔드         우리 것이 떠 있으면 재사용
 5 백엔드 건강     포트가 아니라 /healthz (DB ping 포함)
 6 워커 루프      중복 방지
 7 프론트엔드      **소스가 빌드보다 새로울 때만** 다시 빌드
 8 프론트 건강     307(로그인 리다이렉트)도 정상이다
 9 브라우저       ← 따라잡기 **앞**이다
10 따라잡기       주문 · 클레임 · 오늘 리포트
```

### 6-1. 왜 브라우저를 따라잡기 앞에서 여는가

클레임 조회는 상태 17종 × 창 개수만큼 요청한다. 실측으로 90일치가
**170회 호출 · 약 2분 20초**다. 그동안 화면을 못 보게 막을 이유가 없다.

**서비스가 떴다는 사실과 데이터가 최신이라는 사실은 다르다.** 이 구분이
로컬 운영 모드의 핵심이고, 그래서 브라우저는 서비스 기준으로 열고 데이터
최신성은 마지막에 따로 보고한다.

### 6-2. 스키마가 뒤처졌을 때 자동으로 올리지 않는다

```
BEHIND  → STARTUP_BLOCKED_SCHEMA_BEHIND
          백업 명령과 upgrade 명령을 화면에 찍고 멈춘다
AHEAD   → STARTUP_BLOCKED_SCHEMA_MISMATCH
          코드를 되돌리라고 말한다. downgrade 는 데이터를 잃는다(docs/55 §5)
```

마이그레이션은 되돌리기 어려운 변경이다. 백업 없이 도는 것을 **기본 동작**
으로 만들면 언젠가 그 대가를 치른다.

---

## 7. 두 번 눌러도 안전하다

각 단계가 "이미 우리 것이 떠 있는가" 를 먼저 본다.

```
1회차  DB 재사용 · 백엔드 시작 · 워커 시작 · 프론트 빌드+시작   1분 22초
2회차  전부 재사용                                            2.6초
```

**PID 만 보지 않는다.** PID 는 재사용되므로 재부팅 뒤 같은 번호를 남의
프로그램이 가질 수 있다. `var/run/*.json` 에 **시작 시각**을 함께 적고 둘 다
맞아야 우리 것으로 본다. 안 그러면 종료 스크립트가 남의 프로그램을 죽인다.

포트가 막혀 있는데 우리 기록이 없으면 **그 PID 를 죽이지 않고 실패한다.**

```
[FAIL] 포트 8000 을 다른 프로그램이 쓰고 있습니다: powershell (pid 16248)
       → 그 프로그램을 종료하거나 -BackendPort 로 다른 포트를 지정하세요.
```

---

## 8. 종료 — 무엇을 곱게 세우고 무엇을 안 그러는가

```
1 워커        멈춤 표시 파일 → 이번 바퀴를 마치고 스스로 끝낸다
2 백엔드      종료 요청 → (필요하면) 강제
3 프론트엔드   같음
4 DB          기본적으로 건드리지 않는다
```

### 8-1. 워커만 협조 종료가 필요한 이유

워커만 자기 주기 동안 **쓰기 작업**을 한다. 한가운데서 끊기면 실행 기록이
RUNNING 으로 남고 그걸 치우는 일이 다음 기동의 몫이 된다.

Windows 에는 창 없는 콘솔 프로세스에 보낼 마땅한 종료 신호가 없다.
실측에서 `taskkill` 은 이렇게 답했다.

```
ERROR: The process with PID 16236 could not be terminated.
```

그래서 `var/run/worker.stop` 파일로 협조한다. 워커는 자는 동안에도 2초마다
확인하므로, 10분 주기여도 종료가 10분 걸리지 않는다.

백엔드·프론트는 **그럴 필요가 없다.** 요청 처리 중에 끊기면 PostgreSQL 이
연결 종료와 함께 트랜잭션을 되돌린다. 반쯤 쓰인 상태가 남지 않는다.
그래서 기다리지 않고 바로 종료 절차로 간다.

### 8-2. DB 를 안 내리는 이유

이 PC 의 PostgreSQL 이 Seller OS 전용이라는 보장이 없다. 서비스로 등록돼
있고 다른 프로그램이 붙어 있을 수 있다. 끄는 것은 되돌리기 쉽지만, 남의
작업을 끊는 것은 아니다. 전용이라면 `-StopDatabase` 를 주면 된다.

---

## 9. 겪은 함정 세 가지 (Windows PowerShell 5.1)

이 세 개는 전부 실제로 스크립트를 죽였다. 나중에 고칠 사람을 위해 남긴다.

### 9-1. BOM 없는 UTF-8 은 한글이 깨져 **구문 오류**가 된다

PowerShell 5.1 은 BOM 이 없으면 `.ps1` 을 ANSI 로 읽는다. 주석의 한글이
깨지면서 따옴표 짝이 어긋나 파싱이 실패한다. 모든 `.ps1`/`.psm1` 은
**UTF-8 with BOM** 이어야 한다.

### 9-2. 네이티브 명령의 stderr 가 `$ErrorActionPreference='Stop'` 을 터뜨린다

`& python ... 2>&1` 로 받으면 파이썬 **로그 한 줄**이 ErrorRecord 로 감싸져
스크립트가 그 자리에서 죽는다. 따라잡기가 첫 로그 줄에서 중단됐다.
종료 스크립트는 `taskkill` 의 첫 경고에서 죽어 백엔드·프론트를 남겼다.

→ `Invoke-SellerOsPython` / `Invoke-Taskkill` 이 별도 프로세스로 띄우고
스트림을 파일로 받는다. 종료 코드는 그대로 살려 온다(0/1/2 의 뜻이 다르다).

### 9-3. `npm` 도 같은 부류다 — 진행 상황을 stderr 로 쓴다

`npm run build` 는 Next 가 stdout 을 쓰기 때문에 우연히 통과했다.
`npm install` 은 stderr 로 진행 상황을 쓰므로 **처음 받아 온 저장소에서
스크립트가 죽는다** — node_modules 가 있는 PC 에서는 영영 안 드러난다.
`Invoke-SellerOsNpm` 도 같은 경로를 지나게 했다.

### 9-4. `Start-Process -ArgumentList` 는 따옴표를 붙이지 않는다

배열을 공백으로 잇기만 한다. `-c "코드"` 처럼 공백이 든 인자가 쪼개져
엉뚱한 명령이 되고, **DB 점검이 통째로 실패**했다. `Invoke-SellerOsPython`
이 공백·따옴표가 든 인자를 감싼다.

> 부수 함정: 시작 스크립트가 띄운 자식이 **부모의 stdout 핸들을 물려받아**,
> 시작 스크립트를 파이프로 읽는 쪽이 영원히 끝나지 않았다. 아이콘 실행에서는
> 티가 안 나지만 자동화에서는 멈춘다 — 워커 자식의 출력도 파일로 돌렸다.

---

### 9-5. E2E 와 동시에 돌리지 않는다 (알려진 상호작용)

Playwright 는 포트를 따로 쓰지만(3131/8131) **`frontend/.next` 는 같이
쓴다.** E2E 의 `next dev` 가 그 디렉터리를 쥔 채 기동 스크립트가
`next build` 를 돌리면 둘이 서로의 산출물을 덮어 이렇게 끝난다.

```
Error [PageNotFoundError]: Cannot find module for page: /margin/calculator
```

이번 작업 중 실제로 한 번 겪었다(그리고 E2E 실행도 같이 중단됐다).
막는 장치를 넣지 않았다 — 개발자만 마주치는 상황이고, 막으려면 기동
스크립트가 개발 도구의 상태를 알아야 한다. **E2E 를 돌릴 때는 먼저
`stop_seller_os.ps1`** 을 실행한다.

빌드가 그렇게 깨져도 스크립트는 성공으로 넘어가지 않는다. 종료 코드를
보고 멈추면서 `var/log/frontend-build-*.log` 를 가리킨다.

---

## 10. 보안

```
바인드         127.0.0.1 (기본). `0.0.0.0` 을 쓰지 않는다
외부 노출      없음. 방화벽 규칙을 추가하지 않았다
비밀 전달      전부 자식 프로세스의 환경변수. 명령행에 넣지 않는다
점검 출력      있다/없다 + DB 이름까지. 값도 길이도 안 찍는다
따라잡기 로그   건수와 상태 코드만. 이름·전화·주소·이메일 0
포트 점유자     이름과 PID 만. 명령행은 안 찍는다 (접속 문자열이 섞인다)
```

마지막 두 줄이 테스트로 고정돼 있다
(`test_local_ops_scripts.py`, `Test-LocalOperations.ps1`).
"로그에 비밀을 넣지 말자" 는 규칙은 지켜지지 않는다 — 테스트가 지켜야 한다.

---

## 11. 이번에 만들지 않은 것

```
새 스케줄러 프레임워크        기존 run_workers --once 를 주기로 부를 뿐이다
새 백업 엔진                 run_backup.py 를 그대로 쓴다
Docker                      이 환경에서 못 쓴다(Windows 10 Home)
자동 마이그레이션             BEHIND 면 멈추고 명령을 알려준다
자동 downgrade · DB 초기화    절대
NEXT ENGINE WRITE            0. 읽기만 한다
환불 · 현금 posting           증거 게이트 그대로(docs/84 §6)
과거 날짜 리포트 소급 생성      오늘 것만 만든다
백업 상태 조회 API            §12 참고
```

### 11-1. 백업 상태를 화면에 넣지 않은 이유 (명시적 이탈)

지시서 §44 는 System Status 에 `Backup Last Success` 도 넣으라고 한다.
넣지 않았다.

백업은 파일 시스템에 있고 기존 API 는 그것을 모른다. 넣으려면 **백업 경로를
읽는 endpoint 를 새로 열어야** 하고, 그 응답에는 서버 파일 경로가 들어간다.
로컬 운영에서는 `scripts\status_seller_os.ps1` 이 이미 그 답을 준다.

서버로 옮겨 여러 사람이 볼 때 다시 판단할 일이다.

---

## 12. 실측 결과

### 12-1. 실제 로컬 스모크 (외부 WRITE 0)

```
시작 1회차      1분 22초  DB 재사용 · 백엔드 · 워커 · 프론트 빌드+기동
시작 2회차      2.6초     전부 재사용 · 중복 프로세스 0
전체 시작       2분 30초  (따라잡기 포함)

주문 따라잡기    조회 1 · 신규 0 · 갱신 0 · 변화없음 1        SUCCESS
클레임 따라잡기   조회 16 · 신규 0 · 갱신 0 · 변화없음 16     SUCCESS
                창 10개 (2026-05-19 ~ 08-17, lookback 90일)
중복 주문        0
중복 클레임      0

종료            워커 협조 종료 · 백엔드/프론트 종료 · 포트 반납 · 기록 정리
재시작          start → stop → start 1주기 정상
```

3회 연속 실행에서 신규 0건 — 멱등이 실데이터로 확인됐다.

### 12-2. 실패 경로도 실제로 밟았다

전부 **의도적으로 만들어** 확인했다. 문서로만 적어 둔 것이 아니다.

```
포트를 다른 프로그램이 점유    죽이지 않고 이름·PID 를 알리며 중단
백엔드 강제 종료 후 재시작     낡은 기록을 알아보고 새로 띄운다 (중복 0)
JWT_SECRET 이 기본값          기동 차단 (exit 1)
스키마 BEHIND                기동 차단 + 백업·upgrade 명령 안내
프론트 빌드 실패              성공으로 넘어가지 않고 빌드 로그를 가리킨다
동기화 중 프로세스 중단        RUNNING 으로 남고, 다음 기동이 FAILED 로 정리
```

### 12-3. 자동화된 검사

```
pytest              1497 pass · 5 skip   (2회 반복, 동일)
Vitest              306 pass (29 파일)
tsc --noEmit        clean
next lint           기존 경고 1건 외 없음
next build          clean
Playwright          208 pass (retries=0) · 2회
Test-LocalOperations.ps1   16 pass
ruff                clean
verify_migrations   base→head→직전→head (scratch DB)
```

---

## 13. 사용자가 해야 할 일

```
PC 를 켠다
  ↓
[Seller OS 시작]  ← 바탕화면
  ↓
브라우저가 열린다 (오늘의 운영)
  ↓
창에 따라잡기 결과가 찍힌다
  ↓
PC 를 끄기 전 [Seller OS 종료]
```

바로가기 만들기:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_local_shortcuts.ps1
```

로그인 자동 시작을 원하면(기본은 꺼져 있다):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove
```

### 13-1. "열렸는데 주문 동기화가 실패했다" 면

```
1. scripts\status_seller_os.ps1        무엇이 실패했나
2. 화면 → 운영 → 시스템 상태            데이터 최신성 카드
3. 화면 → 운영 → 예외 센터              원인 코드
4. 자격증명 문제면 화면 → 연동 → 외부 시스템
5. 고친 뒤 scripts\start_seller_os.ps1  (따라잡기가 다시 돈다)
```

**서비스가 떠 있다는 것과 데이터가 최신이라는 것은 다르다.** 시작 스크립트가
마지막에 찍는 `데이터` 줄이 그 구분이다.

---

## 14. 서버로 옮길 때

Core 에 Windows 개념을 넣지 않았다. 바뀌는 것은 런처뿐이다.

| 지금 | 서버 |
|---|---|
| `run_worker_loop.ps1` | systemd timer / cron (docs/49 §1-3 에 이미 있다) |
| `start_seller_os.ps1` | systemd unit / compose |
| 기동 따라잡기 | 그대로 — 재시작마다 돌면 된다 |
| `preflight` · `ops_status` | 그대로 |
| 창 계산 · 멱등 적재 · 잠금 | 손댄 적 없다 |

기동 따라잡기는 서버에서도 쓸모가 있다. 배포로 프로세스가 몇 분 멈춘 뒤
재시작할 때 같은 일을 한다.

---

## 15. 남은 것

```
백업 상태 조회 API                 §11-1 (판단 보류)
external_sync_run.worker_run_id    여전히 비어 있다 (docs/84 §7)
DAILY_REPORT 정책                  이 조직에서 아직 꺼져 있다 — 켜면 따라잡기가 만든다
백업 자동 실행                      Windows 작업 스케줄러 등록은 사용자 몫
                                   (docs/70 §에 등록 명령이 있다)
```
