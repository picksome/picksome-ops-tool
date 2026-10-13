# 55. Migration downgrade 소유권 — "M002 결함" 은 오진이었다

> 조사 결과 문서. 결론부터: **마이그레이션에 결함이 없다.**
> 실패의 원인은 테스트가 seed 소유 조직을 자기 조직처럼 쓴 것이었다.

---

## 1. 무엇을 봤나

지난 세션에 CI 에 `alembic downgrade base` 를 넣으려다 실패했다.

```
psycopg.errors.ForeignKeyViolation:
"organization" 테이블의 자료 삭제, 삭제 작업이 "app_user_org_id_fkey"
외래키 제약 조건 - "app_user" 테이블 - 을 위반했습니다
```

그때 이것을 "M002 baseline seed 의 downgrade 결함" 으로 기록했다.
**그 진단은 틀렸다.**

---

## 2. 실제 원인

### 2-1. M002 는 자기가 만든 것만 지운다

```python
# M002 upgrade
INSERT INTO organization (id, ...) VALUES ('00000000-…-ff', '_SYSTEM_TEMPLATE', …)

# M002 downgrade
DELETE FROM organization WHERE id = '00000000-…-ff'
```

자기가 만든 SYSTEM 템플릿 조직만 지운다. 다른 조직은 건드리지 않는다.
**소유권 관점에서 올바르다.**

### 2-2. 실패한 DB 에는 남의 데이터가 있었다

```
app_user in SYSTEM org : 13
total organization     : 162
```

`picksome_ops_test` 는 여러 통합 테스트가 공유하는 DB 다. 그중 여섯 개 파일이
**M002 가 만든 SYSTEM 조직을 자기 조직처럼 쓴다.**

```
test_approval_executor.py     ORG = 00000000-…-ff
test_approval_governance.py   ORG = 00000000-…-ff
test_approval_workflow.py     ORG = 00000000-…-ff
test_channel_price.py         ORG = 00000000-…-ff
test_channel_price_flow.py    ORG = 00000000-…-ff
test_expense_approval.py      ORG = 00000000-…-ff
```

이 조직을 쓰는 이유는 있다 — M002 가 그 조직에 세금·수수료 정책을 seed 해 두므로,
정책이 필요한 테스트가 그걸 그대로 쓴다.

그 결과 사용자 행이 남고, M002 의 downgrade 가 자기 조직을 지우려 할 때 막힌다.

### 2-3. 빈 DB 에서는 정상 동작한다

```
CREATE DATABASE picksome_fresh
alembic upgrade head     → M036
alembic downgrade base   → 성공, public 스키마에 테이블 0개
alembic upgrade head     → M036
```

---

## 3. 그래서 무엇을 하지 않았나

**`CASCADE` 를 붙이지 않았다.**

붙이면 테스트는 통과한다. 그리고 M002 의 downgrade 가 **남이 만든 사용자를
조용히 지우게 된다.** 지금 막히는 것은 안전장치가 작동하는 것이다.

과거 마이그레이션 파일도 수정하지 않았다. 고칠 것이 없다.

---

## 4. 무엇을 했나

### 4-1. 빈 DB 왕복을 테스트로 고정

`test_migration_data_safety.py`

| 테스트 | 고정하는 것 |
|---|---|
| `test_full_chain_round_trips_on_a_fresh_database` | 빈 DB 에서 head→base→head. base 후 테이블 0개 |
| `test_downgrade_destroys_data_it_does_not_preserve_it` | **왕복 성공 ≠ 무손실** |
| `test_seed_downgrade_refuses_to_delete_rows_it_does_not_own` | 남의 사용자가 있으면 막히는 것이 정상 |

세 테스트는 `fresh_db`(함수 스코프)를 쓴다. 기존 `scratch_db` 는 module 스코프라
앞 테스트가 남긴 행이 그대로 있다 — 빈 상태를 전제하는 검사를 거기서 하면
단독 실행은 통과하고 모듈 전체 실행은 실패한다(실제로 겪었다).

### 4-2. CI 를 전체 왕복으로 되돌림

```yaml
alembic upgrade head
alembic downgrade base
alembic upgrade head
```

CI 의 DB 는 매번 새로 뜨므로 빈 DB 다.

---

## 5. 왕복 성공은 무손실이 아니다

이 구분을 문서 문장이 아니라 테스트로 박아 뒀다.
44번 작업로그가 "downgrade M015 → upgrade head 무손실" 이라고 적은 적이 있는데,
실제 뜻은 "에러 없이 끝났다" 였다.

```
M034 downgrade  →  manual_cash_event 테이블 DROP
                   그 안의 값은 사라진다
M034 upgrade    →  빈 테이블이 돌아온다
                   값은 돌아오지 않는다
```

운영에서 downgrade 를 실행하려는 사람은 이걸 알고 해야 한다.

---

## 6. 남은 것 (이번 범위 아님)

테스트가 seed 소유 조직을 공유하는 구조는 그대로다.

- 장점: M002 가 seed 한 정책을 그대로 쓸 수 있다
- 단점: 공용 테스트 DB 에 SYSTEM 조직 사용자가 쌓이고, 그 DB 에서는
  `downgrade base` 가 막힌다

고치려면 각 테스트가 자기 조직을 만들고 필요한 정책을 직접 seed 해야 한다.
여섯 파일을 건드리는 변경이고, 그 자체로 회귀 위험이 있어 별도 작업으로 둔다.
**지금은 그 사실을 테스트가 명시적으로 문서화한다**
(`test_seed_downgrade_refuses_to_delete_rows_it_does_not_own`).
