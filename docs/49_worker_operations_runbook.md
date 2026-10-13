# 49. Worker 운영 · 백업 복구 런북

> 작성 2026-08-08 · 선행: `48_month_close_orchestration_worklog.md`
>
> **실제 클라우드 인프라를 임의로 구현하지 않았다.** 이 문서는 PostgreSQL 과
> 로컬 파일 스토리지 기준의 절차이며, 운영 환경이 정해지면 그 위에 맞춘다.

---

## 1. Worker 실행

### 1-1. 무엇이 도는가

| worker | 하는 일 | 안 돌면 생기는 일 |
|---|---|---|
| `due_approvals` | 적용일이 도래한 승인을 반영 | 승인만 되고 데이터가 안 바뀐다. 마감 검증이 BLOCKING 으로 막는다 |
| `attachment_delete` | `PENDING_DELETE` 증빙을 실제 삭제 | 지우기로 한 파일이 디스크에 남는다 |
| `channel_publish` | 승인된 가격을 판매처에 반영 | connector 가 없어 현재는 전부 `CONNECTOR_UNAVAILABLE` |
| `period_recalc` | 더러워진 기간의 집계를 다시 계산 | `PERIOD_RECALC_PENDING` 이 계속 마감을 막는다 |
| `operation_issue_scan` | 운영 이슈 스캔 | 이슈 센터가 낡은 목록을 보여준다 |

### 1-2. 실행

```bash
cd backend

# 전체 한 바퀴 — 외부 스케줄러가 이걸 부른다
python -m app.scripts.run_workers --once --trigger SCHEDULED

# 개별
python -m app.scripts.run_workers --worker due_approvals
python -m app.scripts.run_workers --worker operation_issue_scan --org <UUID>

# 상태만 확인 (아무것도 실행하지 않는다)
python -m app.scripts.run_workers --health

# 중단된 프로세스가 남긴 RUNNING 정리
# worker_run 과 external_sync_run **둘 다** 치운다 — 하나만 치우면
# 관리 화면의 절반이 계속 "지금 조회 중" 이라고 말한다 (docs/85 §4)
python -m app.scripts.run_workers --reclaim-only

# 기동 직후 못 돈 기간 따라잡기 (주문·클레임·오늘 리포트)
python -m app.scripts.startup_catchup
```

**종료 코드**

```
0  전부 성공
1  일부 worker 실패 (나머지는 실행됨)
2  실행 자체가 불가능 (DB 접속 실패 등)
```

한 worker 의 실패가 다음을 막지 않는다. 그래서 종료 코드 1은
"아무것도 안 됐다" 가 아니라 "일부가 안 됐다" 이다 — 알림을 그렇게 써야 한다.

### 1-3. 스케줄 예시

애플리케이션 프로세스 안에 스케줄러를 두지 않는다. 워커 프로세스를 여러 개
띄우면 같은 잡이 중복 실행되기 때문이다. 중복 실행은 각 worker 의
advisory lock 이 막지만, 스케줄링 자체는 바깥이 한다.

**cron**

```cron
# 10분마다 전체 한 바퀴
*/10 * * * * cd /srv/picksome/backend && \
  /srv/picksome/venv/bin/python -m app.scripts.run_workers --once \
  --trigger SCHEDULED >> /var/log/picksome/workers.log 2>&1
```

**Windows 개인 PC** (현재 실제 운영 방식 — `docs/85`)

서버가 생기기 전까지는 `scripts/run_worker_loop.ps1` 이 cron 자리를 대신한다.
`scripts/start_seller_os.ps1` 이 이 루프를 띄우고, 루프는 10분마다 위의
`--once` 를 부른다. **새 스케줄러가 아니다** — 기다렸다가 같은 CLI 를 부를 뿐이다.

```powershell
# 직접 띄울 일은 없다. 시작 스크립트가 한다.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_worker_loop.ps1
```

중복 실행은 `var/run/worker.json` 기록으로 막고, 종료는
`var/run/worker.stop` 파일로 협조한다(이번 바퀴를 마치고 끝낸다).
**PC 가 꺼져 있는 동안 안 도는 것은 실패가 아니다** — 다음 기동에서
`app.scripts.startup_catchup` 이 그 기간을 따라잡는다.

**systemd timer** (권장 — 중복 실행을 systemd 가 막는다)

```ini
# /etc/systemd/system/picksome-workers.service
[Unit]
Description=PICKSOME OPS workers

[Service]
Type=oneshot
WorkingDirectory=/srv/picksome/backend
EnvironmentFile=/srv/picksome/backend/.env
ExecStart=/srv/picksome/venv/bin/python -m app.scripts.run_workers --once --trigger SCHEDULED
User=picksome
```

```ini
# /etc/systemd/system/picksome-workers.timer
[Unit]
Description=PICKSOME OPS workers (10분 주기)

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
# 겹침 방지 — 앞 실행이 안 끝났으면 건너뛴다
AccuracySec=30s

[Install]
WantedBy=timers.target
```

### 1-4. 관측

```
GET /api/v1/operations/workers        worker 별 마지막 실행·성공·실패, 대기 깊이
GET /api/v1/operations/workers/runs   실행 이력
POST /api/v1/operations/workers/reclaim-stale
```

확인할 것:

- `never_ran: true` → **스케줄러가 연결되지 않았다.** 가장 흔한 운영 사고다
- `last_success_at` 이 오래됐다 → 계속 실패 중이거나 스케줄이 멈췄다
- `queue_depth` 가 계속 는다 → 처리 속도보다 유입이 빠르다
- `oldest_pending_age_seconds` 가 크다 → 특정 건이 계속 밀리고 있다
- `running_now > 0` 인데 `last_started_at` 이 1시간 전 → 프로세스가 죽었다.
  `reclaim-stale` 이 정리한다

**stack trace 는 저장하지 않는다.** 오류 코드와 한 줄 메시지만 남는다 —
관리 화면에 그대로 노출되면 내부 경로와 SQL 이 새어나가기 때문이다.
상세는 애플리케이션 로그에서 본다.

---

## 2. 운영 배포 점검

### 2-1. 필수 환경변수

| 변수 | 필수 | 비고 |
|---|---|---|
| `DATABASE_URL` | ✅ | `.env.example` 기본값은 **개발용이고 실제와 다르다** |
| `JWT_SECRET` | ✅ | 길고 무작위. 기본값을 그대로 쓰면 토큰을 위조할 수 있다 |
| `APP_ENV` | ✅ | `prod` 에서 debug 응답이 꺼진다 |
| `APP_TIMEZONE` | ✅ | `Asia/Seoul`. 마감 귀속월 판정이 이 값을 따른다 |
| `SETTLEMENT_REOPEN_LIMIT_DEFAULT` | | 기본 3 (D-285) |
| `SMTP_*` | | 알림 EMAIL 채널. 없으면 발송하지 않는다 |
| `EXPENSE_ATTACHMENT_ROOT` | | 증빙 저장 루트. 백업 대상이다 |

⚠️ **`.env.example` 의 값을 그대로 쓰지 않는다.** DB 이름·비밀번호가 실제와
다르고, 그대로 두면 인증 실패로 시간을 버린다(2026-07-30 에 실제로 겪었다).

### 2-2. 배포 순서

```bash
# 1) 마이그레이션 — 애플리케이션보다 먼저
cd backend && alembic upgrade head

# 2) 백엔드
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3) 프론트
cd frontend && npm run build && npm run start

# 4) worker 타이머
systemctl enable --now picksome-workers.timer

# 5) 확인
curl -s localhost:8000/api/health
python -m app.scripts.run_workers --health
```

**worker 타이머를 켜는 것을 잊지 않는다.** 백엔드와 프론트만 띄우면 화면은
정상으로 보이지만 승인이 반영되지 않고 이슈가 갱신되지 않는다.

---

## 3. 백업 · 복구

### 3-1. 무엇을 백업하나

| 대상 | 방법 | 빠뜨리면 |
|---|---|---|
| PostgreSQL | `pg_dump -Fc` | 전부 잃는다 |
| 증빙 파일 (`EXPENSE_ATTACHMENT_ROOT`) | 파일 동기화 | DB 에는 첨부 행이 있는데 파일이 없다 |
| **암호화 키** (vault) | **별도 보관** | DB 를 복구해도 **금고를 열 수 없다** |

⚠️ **암호화 키를 DB 백업과 같은 곳에 두지 않는다.** 같이 유출되면 암호화가
아무 의미가 없고, 따로 잃어버리면 복구된 DB 에서 자격증명을 읽을 수 없다.

### 3-2. 백업

```bash
# DB — 커스텀 포맷(-Fc)이라 부분 복구가 가능하다
pg_dump -Fc -h "$PGHOST" -U picksome picksome_ops \
  -f "backup/picksome_$(date +%Y%m%d_%H%M).dump"

# 증빙 파일 — 삭제를 전파하지 않는다(--delete 를 쓰지 않는다).
# 운영에서 실수로 지운 파일이 백업에서도 사라지면 복구할 수 없다.
rsync -a "$EXPENSE_ATTACHMENT_ROOT/" backup/attachments/
```

### 3-3. 복구 순서

**순서가 중요하다.**

```
1) 빈 DB 생성
2) pg_restore
3) alembic current 로 리비전 확인
4) 필요하면 alembic upgrade head
5) 증빙 파일 복원
6) 암호화 키 복원
7) 검증 (아래)
8) worker 재시작
```

```bash
createdb -h "$PGHOST" -U postgres picksome_ops_restored
pg_restore -h "$PGHOST" -U postgres -d picksome_ops_restored --no-owner backup/xxx.dump

DATABASE_URL="postgresql+psycopg://...picksome_ops_restored" alembic current
DATABASE_URL="postgresql+psycopg://...picksome_ops_restored" alembic upgrade head
```

**마이그레이션 호환성**: 백업이 M028 시점이고 코드가 M030 이면 `upgrade head`
가 필요하다. 반대로 **백업이 코드보다 최신이면 복구하지 않는다** —
downgrade 로 맞추려다 데이터를 잃는다. 코드를 먼저 그 리비전으로 되돌린다.

### 3-4. 복구 후 검증

```sql
-- ① 확정 snapshot 이 살아 있는가 (확정 숫자의 근거)
SELECT year_month, generation, net_profit, content_hash
  FROM monthly_close_snapshot WHERE superseded_at IS NULL ORDER BY year_month DESC;

-- ② 확정 숫자가 raw 데이터와 어긋나지 않는가
--    (애플리케이션에서: GET /api/v1/monthly-close/{ym}/drift)

-- ③ 첨부 행은 있는데 파일이 없는 경우
SELECT COUNT(*) FROM expense_attachment WHERE status = 'STORED' AND deleted_at IS NULL;
--    → 그 수만큼 실제 파일이 있는지 스토리지에서 확인

-- ④ 지급 금액 일관성 — 트리거가 관리하는 값이라 복구 후에도 맞아야 한다
SELECT i.id, i.paid_amount, COALESCE(SUM(p.amount), 0) AS actual
  FROM payout_item i
  LEFT JOIN payout_payment p
         ON p.payout_item_id = i.id AND p.deleted_at IS NULL
 GROUP BY i.id, i.paid_amount
HAVING i.paid_amount <> COALESCE(SUM(p.amount), 0);
--    → 0행이어야 한다

-- ⑤ 중단된 worker 실행 정리
--    python -m app.scripts.run_workers --reclaim-only
```

**운영 DB 에서 복구를 테스트하지 않는다.** 반드시 별도 DB 로 복원해 검증한다.

### 3-5. 자동화

`backend/scripts/e2e_db.py` 가 이미 "빈 DB 생성 → `alembic upgrade head` →
시드" 를 수행한다. dump→restore smoke test 는 그 위에 얹을 수 있지만
**아직 자동화하지 않았다** — 운영 백업 위치가 정해지지 않아 대상이 없다.

---

## 4. 정기 점검

| 주기 | 항목 | 확인 방법 |
|---|---|---|
| 매일 | worker 실행 여부 | `--health` 의 `last_success_at` |
| 매일 | 차단 이슈 | `GET /operations/issues/summary` 의 `blocking` |
| 매주 | 백업 복원 테스트 | 별도 DB 로 `pg_restore` + §3-4 |
| 월말 | 마감 검증 | `POST /monthly-close/{ym}/validate` |
| 확정 후 | drift | `GET /monthly-close/{ym}/drift` |
