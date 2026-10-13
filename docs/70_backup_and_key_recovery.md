# 70. 백업 자동화 · 마스터 키 복구

2026-08-11

`docs/25_devops.md §4-2` 의 정책을 **실행 가능한 형태로** 옮긴 것이다. 보존
기간을 새로 만들지 않았다.

---

## 1. 무엇이 준비돼 있나

| | 상태 |
|---|---|
| 백업 실행 스크립트 | `backend/scripts/run_backup.py` — 검증·회전 포함 |
| 복원 검증 스크립트 | `backend/scripts/verify_backup_restore.py` — 일회용 DB |
| 실제 백업 스모크 | **통과** (921KB, 검증·회전 동작) |
| 실제 복원 검증 | **통과** (revision·행수·조인 스모크) |
| 자동 스케줄 | **`READY_FOR_PRODUCTION_INSTALL`** — 아래 설치 명령 참고 |

`ACTIVE_LOCAL` 이 아니다. 이 개발 PC 에 cron/작업 스케줄러를 등록하지 않았다 —
개발자 PC 는 백업 대상 서버가 아니다. 스크립트는 손으로 한 번 돌려 확인했다.

---

## 2. 보존 정책 (변경 금지)

```
backup/daily/     7개    매일
backup/weekly/    4개    일요일
backup/monthly/   6개    매월 1일
```

`run_backup.py` 의 `RETENTION` 이 이 표다. 바꾸려면 `docs/25_devops.md` 를 먼저
고친다.

같은 날 두 번 돌면 **덮어쓴다.** 하루치가 여러 개 쌓이면 "7개" 가 며칠치인지
알 수 없어진다.

---

## 3. 실행

```bash
cd backend
python -m scripts.run_backup              # 날짜로 종류 자동 판단
python -m scripts.run_backup --kind daily # 특정 종류만
python -m scripts.run_backup --dry-run    # 무엇을 할지만 출력
```

`pg_dump` 가 PATH 에 없으면 `PG_BIN` 으로 알려 준다. 저장 위치는
`BACKUP_ROOT`(기본 `backup/`).

### 성공을 가장하지 않는다

파일이 만들어졌다고 성공이 아니다. 셋을 확인한다.

```
1. pg_dump 종료 코드 0
2. 파일 크기 > 0
3. pg_restore --list 로 목차가 읽히는가
```

3이 핵심이다. 디스크가 가득 차면 `pg_dump` 가 0 을 반환하면서 **잘린 파일**을
남기는 경우가 있고, 그건 복원할 때가 되어서야 드러난다. 실제로 손상 파일을
넣어 `UNREADABLE_ARCHIVE` 로 잡히는 것을 확인했다.

검증에 실패한 파일은 **지운다.** 남겨 두면 다음 회전에서 정상 백업을 밀어낸다.

### 출력

한 줄 JSON. 비밀은 없다.

```json
{"kind":"daily","result":"OK","path":"backup/daily/picksome_ops_2026-08-11.dump",
 "size_bytes":921645,"kept":7,"rotated_out":[]}
```

실패하면 `result=FAILED` 와 `error_category` 가 나오고 **종료 코드가 1** 이다.
스케줄러가 그걸 보고 알린다.

---

## 4. 설치 (운영 서버에서)

### Linux (cron)

```cron
# 03:00 KST 매일. 종류는 스크립트가 날짜로 판단한다.
0 3 * * *  cd /srv/picksome/backend && \
           /usr/bin/python3 -m scripts.run_backup >> /var/log/picksome-backup.log 2>&1
```

### Windows (작업 스케줄러)

```powershell
schtasks /Create /TN "PicksomeBackup" /SC DAILY /ST 03:00 ^
  /TR "cmd /c cd /d D:\picksome\backend && python -m scripts.run_backup"
```

### 설치 확인

```bash
python -m scripts.run_backup --dry-run     # 경로·종류가 맞나
ls -la backup/daily/                       # 다음 날 파일이 생겼나
```

### 주 1회 복원 검증

```bash
python -m scripts.verify_backup_restore
```

일회용 DB(`picksome_restore_test_<난수>`)를 만들어 복원하고 지운다. 이름이
`_restore_test_` 를 포함하지 않으면 **거부한다** — 개발·운영 DB 에 덮어쓸 수 없다.

---

## 5. 마스터 키

### 어디에 있나

```
DB (credential_secret.ciphertext)   암호문
환경변수 CREDENTIAL_MASTER_KEY       현재 키
환경변수 CREDENTIAL_RETIRED_KEYS     은퇴한 키 (쉼표 구분, 읽기 전용)
```

**둘을 같은 곳에 두지 않는다.** 백업 하나가 새면 전부 풀리기 때문이다.
백업 덤프에 키가 없는 것을 `verify_backup_restore.py` 가 매번 확인한다.

### 키를 절대 넣지 않는 곳

```
DB dump          backup/daily|weekly|monthly
Git              docs (평문)
Docker image     로그 · 감사 · 예외 메시지
```

`backup/` 과 `*.dump` 는 `.gitignore` 에 있다. 예전에는 `*.sql.gz` 만 막고
있어서 확장자나 위치가 조금만 달라도 커밋될 수 있었다.

### 키 백업 (사람이 해야 하는 일)

이건 스크립트가 대신할 수 없다. **DB 백업과 물리적으로 다른 곳**에 둔다.

```
비밀번호 관리자(Bitwarden 등)의 별도 항목
또는 봉인된 오프라인 사본
```

키를 바꿀 때마다 옛 키를 `CREDENTIAL_RETIRED_KEYS` 에 남기고 백업도 갱신한다.

### 복구 절차

```
1. 새 DB 준비
2. pg_restore 로 덤프 복원
3. CREDENTIAL_MASTER_KEY 를 별도 보관처에서 가져와 환경변수로 주입
4. 앱 기동 → 자격증명 복호화 확인
```

3을 못 하면 **자격증명은 영영 못 푼다.** 판매·비용·정산 데이터는 살아 있고
외부 연동 자격증명만 다시 발급받아야 한다.

### 검증된 것

`app/tests/integration/test_master_key_recovery.py` 8건.

```
암호문에 평문 없음
지문 ≠ 키
틀린 키 → 복호화 실패, 예외 메시지에도 값 없음
변조 → 거부 (Fernet 인증 암호)
은퇴 키로 옛 암호문 읽힘 / 새 암호문은 새 키로만
키 없음·형식 오류 → fail closed (평문 fallback 없음)
DB 어느 텍스트 컬럼에도 키 없음
```

---

## 6. 남은 것

| 항목 | 상태 |
|---|---|
| 운영 서버 cron 등록 | 서버가 정해지면 §4 명령으로 |
| 원격 보관(오프사이트) | 미정 — 현재는 로컬 디스크 |
| 백업 실패 알림 연동 | 종료 코드는 나오지만 알림 채널 미연결 |

오프사이트 사본이 없으면 디스크가 통째로 죽었을 때 백업도 함께 죽는다.
운영 데이터를 넣기 전에 정해야 한다.
