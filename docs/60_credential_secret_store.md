# 60. 자격증명 저장소 — 설계 · 운영 · 백업

M042 (`credential_secret`), M043 (`supplier_import_run`) · 2026-08-10

---

## 1. 왜 만들었나 — 기존 vault 로는 무인 동기화가 안 된다

`password_vault_entry` 는 **사람이 읽는 쪽지**다.

```
service_name     "쿠팡 Wing"
vault_provider   "Bitwarden"
vault_item_name  "쿠팡 Wing 항목"
account_masked   "pic****me"
```

비밀이 한 글자도 없고, 들어가지 않도록 CHECK 제약까지 걸려 있다(M001). 사람이
찾아가는 용도로는 옳다. 그런데 **새벽 3시에 도는 배치는 쪽지를 읽을 수 없다.**

그래서 둘로 나눴다. 합치지 않은 이유는 목적이 다르기 때문이다.

| | 무엇 | 복호화 |
|---|---|---|
| `password_vault_entry` | 사람이 찾아갈 위치 | 불가 (값이 없다) |
| `credential_secret` | 기계가 쓰는 인증정보 | 가능 (암호문 보관) |

화면에서도 둘을 나눠 놓았다. 쪽지만 있는 상태를 "설정됨" 이라고 말하면 사용자는
동기화가 왜 계속 실패하는지 알 수 없다.

---

## 2. 구조

```
external_account.credential_secret_ref   불투명 참조 (API 응답 금지)
            ↓
SecretStore (인터페이스)                  put / get / delete / exists
            ↓
LocalEncryptedSecretStore                 credential_secret 테이블 + Fernet
InMemorySecretStore                       테스트 전용 (운영 등록 차단)
```

호출자는 `external_account_id` 와 `credential_kind` 만 안다. 저장 위치·키·암호문은
모른다. `core/attachment_storage.py` 와 같은 구조다 — 관리형 저장소로 갈 때
`SecretStore` 구현을 하나 더 만들면 되고 호출부는 바뀌지 않는다.

### 커넥터는 저장소를 모른다

```
Sync Service → Credential Resolver → SecretStore → dict → Connector
```

커넥터에 DB 세션도 저장소 구현도 넘기지 않는다. 넘기면 셀러툴을 붙일 때마다
"이 커넥터가 비밀을 어디까지 볼 수 있나" 를 검토해야 한다. 커넥터는 쓸 값이 담긴
dict 하나만 받는다.

`ResolvedCredential` 은 dataclass 가 **아니다.** 자동 생성된 `__repr__` 이 값을
그대로 찍기 때문이다 — `print(dto)` 한 번이면 CI 로그에 남는다.

---

## 3. 암호화

`cryptography` 의 **Fernet** 을 쓴다. 이미 의존성에 있다
(`python-jose[cryptography]` 경유, 46.0.7).

| | |
|---|---|
| AES-128-CBC + HMAC-SHA256 | 인증된 암호화 — 변조를 복호화 전에 잡는다 |
| IV 를 라이브러리가 생성 | nonce 재사용 실수를 할 여지가 없다 |
| 토큰에 버전·IV·HMAC 포함 | nonce 컬럼을 따로 둘 필요가 없다 |
| MultiFernet | 키 교체 기간에 옛 키로도 읽는다 |

AES-GCM 도 후보였지만 **nonce 를 우리가 만들어야 한다.** GCM 에서 nonce 재사용은
치명적이고, 그 실수를 할 수 있는 코드를 굳이 쓸 이유가 없다. Fernet 은
`cryptography` 가 "직접 고르고 싶지 않은 사람을 위해" 내놓은 recipes 계층이다.

암호 primitive 를 직접 구현하지 않았다.

### payload 전체를 암호화한다

```json
{ "access_key": "...", "secret_key": "..." }
```

이 JSON 을 통째로 암호화한다. 항목 이름조차 평문으로 남지 않는다. vendor 별
컬럼(`next_engine_password`, `coupang_secret_key`)을 만들지 않는 이유이기도 하다 —
셀러툴 하나마다 스키마가 늘고 Core 가 vendor 를 알게 된다.

지원 형태:

```
API_KEY             api_key
ACCESS_SECRET       access_key, secret_key
CLIENT_CREDENTIALS  client_id, client_secret
OAUTH_REFRESH       client_id, client_secret, refresh_token
BASIC               username, password
CUSTOM              (자유)
```

필수 항목은 **저장 전에** 검증한다. 실행 시점에야 알게 되면 그때는 새벽 배치가
실패한 뒤다. 검증 메시지는 어떤 **칸**이 비었는지만 말하고 값은 말하지 않는다.

---

## 4. 마스터 키

```
CREDENTIAL_MASTER_KEY      현재 키. 이걸로 암호화한다
CREDENTIAL_RETIRED_KEYS    쉼표 구분. 읽기 전용 (교체 기간용)
CREDENTIAL_STORE_BACKEND   local (지금은 이것뿐)
```

새 키 만들기:

```
python -c "from app.core.secret_store import generate_master_key as g; print(g())"
```

`credential_secret.key_fingerprint` 는 SHA-256 앞 16자다. **키가 아니다.** 32바이트
난수 키의 해시 앞부분으로 키를 되찾을 수 없다. "옛 키로 암호화된 행이 아직
남았는가" 를 복호화 없이 세기 위한 것이다.

### 키가 없으면 실패한다

`503 CREDENTIAL_STORE_NOT_CONFIGURED`. 코드 버그가 아니라 배포 설정이 덜 된
상태라 500 이 아니고, 운영자가 할 일이 명확하다.

**평문으로 대신 저장하는 fallback 은 없다.** 그런 fallback 은 한 번 켜지면 아무도
다시 끄지 않는다.

---

## 5. ⚠️ 백업 절차 — 여기가 바뀌었다

이 구현의 보안은 **분리 하나**에 달려 있다.

```
DB           암호문
환경변수      마스터 키
```

DB 덤프만 유출돼도 복호화되지 않는다. 그래서:

> **DB 백업과 마스터 키를 같은 번들·같은 저장소·같은 접근권한 아래 두지 않는다.**

같은 곳에 두면 이 분리가 무의미해지고, "암호화했다" 는 말만 남는다.

권장:

```
DB 덤프          기존 백업 경로 그대로
마스터 키         비밀번호 보관함(Bitwarden 등)에 별도 보관
                 접근 권한을 DB 백업과 다르게 준다
```

**키를 잃으면 자격증명을 복구할 수 없다.** 암호문은 남지만 아무도 못 읽는다.
그때는 각 외부 계정에 자격증명을 다시 입력하는 수밖에 없다 — 되돌리기가 아니라
재설정이다. 키 백업은 DB 백업만큼 중요하다.

### 복원 시 확인

DB 만 복원하고 키를 안 옮기면 계정은 "설정됨" 인데 실제로는 못 읽는 상태가 된다.
화면은 계속 "설정됨" 이라고 말하므로 사람이 먼저 알아차릴 방법이 없다. 그래서
마감 점검에 `CREDENTIAL_REFERENCE_BROKEN` 을 넣었고, 연결 시험은
`CREDENTIAL_BROKEN` 을 미설정과 **다른 상태**로 보고한다.

### 키 교체 절차

1. 새 키 생성
2. `CREDENTIAL_RETIRED_KEYS` 에 **옛 키를 먼저 넣는다**
3. `CREDENTIAL_MASTER_KEY` 를 새 키로 바꾼다
4. 재기동. 이 시점부터 새로 저장되는 것은 새 키로 암호화된다
5. 기존 행은 옛 키로 계속 읽힌다. 각 계정을 한 번씩 교체하면 새 키로 넘어간다
6. `key_fingerprint` 로 옛 키 행이 0 이 되면 은퇴 키를 제거한다

2번을 건너뛰면 기존 암호문을 전부 못 읽는다.

---

## 6. 남기지 않는 것

| 자리 | 내용 |
|---|---|
| API 응답 | `credential_configured` 불리언, 종류, 변경 시점만 |
| 검증 오류 | 제출값(`input`)을 항상 제거 (`core/redaction.py`) |
| 감사 로그 | `CREDENTIAL_CONFIGURED/ROTATED/REMOVED` 사실만 |
| 요청 로그 | **본문을 읽는 미들웨어가 없다** — 흘러갈 경로 자체가 없다 |
| sync_run | 외부 예외는 타입 이름만 (`RuntimeError`) |
| 되읽기 | 없다. `[비밀번호 보기]` 를 만들지 않았다 |

마지막이 중요하다. 되읽기 경로를 만들면 그게 가장 약한 고리가 된다. 잊었으면
새로 넣는다.

요청 로그에 대한 방어는 redaction 이 아니라 **구조**다. 미들웨어가 하나라도
본문을 읽기 시작하면 그때부터 모든 endpoint 가 비밀 취급 대상이 된다. 그래서
`test_no_middleware_reads_the_request_body` 가 미들웨어가 늘어나면 먼저 깨진다.

---

## 7. 실패 상태를 뭉치지 않는다

```
CONNECTOR_UNAVAILABLE       우리가 아직 구현 안 함        (501)
CREDENTIAL_NOT_CONFIGURED   설정이 안 됨                 (422)
CREDENTIAL_BROKEN           참조는 있는데 못 읽음         ← M042 에서 추가
AUTHENTICATION_FAILED       값이 틀림
CONNECTION_FAILED           네트워크·상대 서버
RATE_LIMITED                잠시 후 다시
```

`CREDENTIAL_BROKEN` 을 "미설정" 으로 뭉개면 사용자는 다시 설정하고, 원인(키 교체
실수·DB 만 복원)은 그대로 남는다.

화면은 네 가지를 **따로** 보여준다.

```
연동 구현    사용 가능 / 미구현
자격증명     설정됨(종류·변경일) / 미설정
연결 시험    정상 / 사유별 실패 / 아직 해 보지 않음
보관함 쪽지   연결됨(사람용) / 없음
─────────────────────────────────
동기화       사용 가능 / 사용 불가(이유)
```

하나로 뭉쳐 "연결됨/안 됨" 으로 보여주면 사용자는 자기 자격증명을 몇 번씩 다시
넣는다 — 실제로는 우리가 아직 안 만든 경우가 가장 흔한데도.

---

## 8. 공급처 Excel 반영 (M043)

세 단계로 나눴다.

```
ANALYZE   파일만 읽는다. DB 를 조회하지 않는다
DRY_RUN   DB 와 대조해 무엇이 바뀔지 계산한다 (기본)
APPLY     실제로 반영한다
```

`--apply` 불리언 하나로 운영 데이터가 바뀌는 구조는 실수를 부른다. **운영에서는
`APPLY` 가 막힌다** — CLI 한 줄로 운영 공급처 마스터가 바뀌는 경로를 남기지
않는다. 미리보기는 운영에서도 된다(못 하게 하면 사람이 결국 우회한다).

`supplier_import_run` 에 파일 내용 SHA-256, 실행 환경, 건수, **버린 자격증명 열의
개수**를 남긴다. 파일명은 바뀌고 Excel 은 열기만 해도 수정 시각이 바뀐다 —
내용 해시만이 안정적이다.

### 실제 반영 결과 (개발 DB)

```
1회차   신규 54 · 변화 없음 0
2회차   신규  0 · 변화 없음 54     ← 멱등
supplier_site 총 54건 · 중복 0
supplier_site / audit_log 에 평문 비밀번호 0건 (실제 값으로 검색해 확인)
```

Excel 의 `ID`/`PW` 열은 읽어서 버린다. 남는 것은 `credential_present` 불리언뿐이다.

---

## 9. 첫 Adapter — **차단됨**

착수 조건 네 가지를 확인했다.

| 조건 | 상태 |
|---|---|
| 공식 API 문서 | ❌ `external_system.documentation_url` 이 둘 다 NULL |
| Credential | ❌ env·`credential_secret`·`external_account` 모두 0건 |
| 필요한 API 권한 | ❌ 확인할 대상이 없다 |
| 실제 raw 응답 샘플 | ❌ 없다 |

```
BLOCKED_CREDENTIAL
BLOCKED_API_SAMPLE
```

**Adapter 를 구현하지 않았다.** 실제 응답 없이 만들면 매핑이 추측이 되고, 응답이
다를 때 조용히 틀린 값이 Core 로 들어온다. 지금은 그 상태를 화면이 정직하게
말하는 것까지가 최선이다.

착수 가능해지는 시점: 위 넷 중 **문서 + credential + 샘플 응답** 이 갖춰졌을 때.
그때 순서는 read-only capture → sanitized fixture → DTO(실제 필드만) →
상품 매핑 → 멱등 재동기화다.

---

## 10. Playwright 간헐 timeout — 가설 검증 결과

지난 세션의 유력 가설은 "`next dev` 의 라우트 on-demand 컴파일" 이었다.
**측정으로 검증했고, 지지되지 않는다.**

정상 실행의 컴파일 시간(전체 스위트 로그에서):

```
/                  996ms      /expenses          509ms
/dashboard        1851ms      /approvals         352ms
/products          848ms      /operations/system 489ms
```

`.next` 캐시를 통째로 지운 **콜드 상태**에서도:

```
/            2.9s
/dashboard   4.3s
/expenses    732ms   ← 문제의 테스트가 쓰는 라우트
```

17.7초를 46.8초로 만들려면 약 29초가 더 필요하다. 가장 느린 콜드 컴파일이
4.3초고, 해당 라우트는 0.7초다. **컴파일로는 설명되지 않는다.**

구간 계측 기준값(`E2E_TIMING_VERBOSE=1`):

```
총 3491ms — reseed 1767 / 페이지 이동 882 / DB 확인 322 / 나머지 각 100ms 내외
```

그래서:

- timeout 을 늘리지 않았다 (증상만 밀어낸다)
- retry 를 켜지 않았다 (빨간 불을 초록으로 덮는다)
- `next build` + `next start` 로 E2E 를 바꾸지 않았다 — 컴파일이 원인이 아닌 것으로
  나왔으므로, 그 구조 변경은 원인과 무관한 작업이 된다

**원인 미확정으로 유지한다.** 계측은 남아 있으므로 다음에 재현되면 어느 구간이
늘었는지 첨부된 표가 말해 준다.

---

## 11. 다음

1. 실제 셀러툴 API 문서·credential·응답 샘플 확보 (§9 차단 해제 조건)
2. 관리형 secret 저장소가 필요해지면 `SecretStore` 구현 추가 — 호출부는 그대로
3. 운영 공급처 반영을 위한 명시적 관리자 흐름 (지금은 개발 CLI 만)
4. Playwright timeout 재현 시 구간 표 확인
