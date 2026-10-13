# 58. External System 기반 설계 (M038 · M039)

> 특정 셀러툴을 붙이기 전에 **공통 계약**부터 세운다.
> 이 문서의 결정은 NEXT ENGINE 이든 윈들리든 그 다음 무엇이든 똑같이 적용된다.

---

## 1. 기존 integration 계층을 먼저 읽었다

| 테이블 | 실제 의미 |
|---|---|
| `integration_account` | **판매처의 우리 판매자 계정.** `sale_channel_id` 를 갖는다 |
| `integration_job` | 그 계정의 예약 작업 |
| `external_product_mapping` | 판매처 상품 ↔ 우리 상품 (M001, **행 0건 · 참조 코드 0곳**) |
| `sales_order.integration_account_id` | 이 주문이 어느 스토어 것인가 |
| `marketplace_settlement_record.integration_account_id` | 이 정산서가 어느 스토어 것인가 |

`integration_account` 의 인증은 **inbound** 다 — 우리가 API 키를 발급하고
봇이 우리에게 들어온다(`auth_kind`, `api_key_hash`).

---

## 2. Marketplace Account ≠ Seller Tool Account

```
쿠팡 본점 / 스마트스토어 A     주문·정산이 귀속된다. 판매채널이 있다. inbound
NEXT ENGINE 회사계정          주문을 소유하지 않는다. 채널이 없다. outbound
```

### 선택지 비교

| | 내용 | 판단 |
|---|---|---|
| **A** | `integration_account` 를 모든 외부 시스템에 확장 | ❌ |
| **B** | 판매처는 그대로, 셀러툴은 새 `external_account` | ✅ **채택** |
| **C** | 상위 generic 계정 + 두 종류가 확장 | ❌ 지금은 |

### A 를 버린 이유

여섯 테이블이 `integration_account` 를 FK 로 참조한다.

```
sales_order · marketplace_settlement_record · product_channel_listing
channel_price_publish_outbox · integration_job · external_product_mapping
```

확장하면 **주문이 NEXT ENGINE 계정에 귀속될 수 있게 된다.** `sale_channel_id`
도 셀러툴에는 의미가 없어 NULL 로 남고, 그러면 "채널 없는 계정" 이라는 예외
분기가 판매·정산 코드 전반에 생긴다.

### C 를 버린 이유

위 여섯 FK 를 전부 새 상위 테이블로 옮겨야 한다. 지금 얻는 것은 개념적 정돈
하나뿐이고, 잃을 수 있는 것은 주문·정산 귀속의 정확성이다. 나중에 은행·카드
같은 시스템이 실제로 들어올 때 다시 판단한다.

### `external_product_mapping` 을 왜 쓰지 않았나

`integration_account` 에 묶여 있고 PRODUCT 전용이며 `org_id` 가 없다.
행 0건, 참조 코드 0곳 — 실제로 쓰인 적이 없는 M001 자리다.
거기에 셀러툴 매핑을 넣으면 셀러툴 계정이 판매처 계정이어야 한다는 뜻이 된다.
**건드리지 않고 남겨 뒀다.** 정리는 판매처 매핑 요구가 확정될 때 판단한다.

---

## 3. 스키마

### `external_system` — 전역 카탈로그

`org_id` 가 없다. "NEXT ENGINE 이라는 서비스가 존재한다" 는 조직마다 다르지 않다.

**enum 이 아니라 행이다.** enum 이면 셀러툴 하나 추가에 마이그레이션이 필요하고,
조직마다 쓰는 도구가 다른 SaaS 에서 그건 배포 없이 아무것도 못 붙인다는 뜻이다.

### `external_account` — 조직별

한 조직이 같은 셀러툴 계정을 여럿 가진다(법인 분리 등).
`(org_id, external_system_id, account_code)` UNIQUE.

**비밀 컬럼이 없다.** `credentials_vault_entry_id` 로 보관 위치만 가리킨다.
API 응답은 `credential_configured`(불리언)만 준다 — vault 항목 id 조차 나가지 않는다.

계정 등록과 자격증명 설정을 나눴다. 등록 폼에 비밀번호 칸을 두면 그 값이
요청 로그·브라우저 히스토리에 남는다.

### `external_entity_mapping`

```sql
CREATE UNIQUE INDEX uq_eem_external_identity
    ON external_entity_mapping
       (external_account_id, external_entity_type, external_entity_id)
 WHERE deleted_at IS NULL
```

**한 외부 항목은 내부 하나에만 붙는다.** 뚫리면 같은 외부 상품의 재고·원가가
두 상품으로 갈라져 들어온다. 반대(한 상품에 여러 외부 매핑)는 허용한다.

`internal_entity_id` 에 FK 를 걸지 않았다 — `entity_type` 마다 대상 테이블이
다르기 때문이다. 대신 서비스가 존재를 검증하고 고아 매핑은 데이터 품질이 잡는다.

현재 `entity_type` 은 `PRODUCT` 뿐이다. **지원하지 않는 유형을 미리 열지 않았다** —
열어 두면 "매핑은 됐는데 아무 일도 안 일어나는" 상태가 생긴다. 늘릴 때 CHECK 를
함께 늘린다. (카테고리 매핑도 같은 방식으로 확장 가능하다.)

### `external_sync_run` / `external_sync_conflict` / `external_field_authority`

`worker_run` 을 재사용하지 않은 이유: worker_run 은 "어떤 worker 가 언제 돌았나"
이고 계정 개념이 없다. 계정별 마지막 성공·충돌 건수를 `summary_json` 에 넣으면
조회마다 JSON 을 파싱해야 하고 인덱스도 못 건다. 대신 worker 가 돌린 sync 는
`worker_run_id` 로 이어 둔다.

---

## 4. 매핑과 provenance 는 다른 것이다

```
매핑        외부 객체 A = 내부 객체 B      (동일성)
provenance  이 값이 어디서 관측됐는가      (출처)
```

매핑 테이블은 **동일성만** 담는다. 출처는 각 도메인이 이미 갖고 있다
(`product_source_cost_version.source_type`, `fx_rate_snapshot.source_type`).

모든 업무 테이블에 `source_*` 컬럼을 뿌리거나 범용 EAV 를 만들지 않았다 —
지금 그것을 요구하는 use case 가 없다.

---

## 5. 커넥터는 계약뿐이다

```
Connector      외부 API → DTO           (vendor 지식이 여기서 끝난다)
Sync Service   DTO → 검증 → 매핑 → Core  (vendor 를 모른다)
```

커넥터가 업무 테이블을 직접 쓰면 vendor 로직이 Core 로 샌다.

**실제 API 를 호출하는 코드가 없다.** credential 과 실제 응답 샘플 없이
문서 기억으로 구현하면 응답이 다를 때 조용히 틀린 값이 들어온다.

| 상태 | 응답 |
|---|---|
| 커넥터 미구현 | `CONNECTOR_UNAVAILABLE` **501** |
| capability false | `UNSUPPORTED_CAPABILITY` **422** |

`capabilities_of()` 는 **전 capability 를 True/False 로** 준다. 지원하는 것만
주면 화면이 "목록에 없음" 과 "지원 안 함" 을 구분하지 못한다.

테스트 전용 커넥터는 `register(..., allow_in_production=False)` 로만 등록되고
`APP_ENV=prod` 에서는 예외가 난다. 운영에서 가짜 커넥터가 성공을 돌려주면
사용자는 동기화가 되고 있다고 믿는다.

---

## 6. 덮어쓰지 않는다

### 필드 권위

`external_field_authority` 에 `MANUAL_OVERRIDE` 가 있으면 sync 가 덮지 않고
`external_sync_conflict` 로 남긴다.

덮으면 사용자는 같은 수정을 매번 반복하다 **동기화를 꺼 버린다.**
audit_log 를 매 sync 마다 뒤지는 방법도 있지만 느리고 취약해서, 필드 단위로
한 줄만 둔다.

### MVP 규칙

```
권위 기록 없음        → 외부 값을 반영한다 (첫 import)
MANUAL_OVERRIDE      → 덮지 않고 충돌로 남긴다
```

전체 policy engine 은 만들지 않았다. 실제 충돌 패턴을 보고 늘린다.

### 원가는 특별 취급

```
셀러툴이 준 매입가  →  product_source_cost_version  (관측 사실, source_type=SELLER_TOOL)
                    ✗  product.cost_amount          (채택한 값 — COST_CHANGE 승인만)
```

주 소싱처가 없으면 아무것도 하지 않는다. **공급처를 추측해서 만들지 않는다.**

---

## 7. 동기화 트랜잭션

**행 단위 커밋**이다. 상품 1,000건 중 1건이 잘못됐다고 999건을 되돌리면
운영이 안 된다. 실패는 세어서 실행 기록에 남긴다.

`status` 는 `SUCCESS` / `PARTIAL` / `FAILED` / `UNSUPPORTED` 를 구분한다.
건수는 **미완료일 때 NULL** 이다 — 0 으로 두면 "아무것도 못 가져왔다" 로 읽힌다.

**내부 상품을 새로 만들지 않는다.** 외부에 있는데 우리에게 없으면 `skipped` 다.
셀러툴이 Seller OS 의 master 가 아니다.

---

## 8. 권한

```
조회        ADMIN / OPERATOR / SOURCING
계정 관리    ADMIN
동기화 실행  ADMIN / OPERATOR
```

자격증명을 다루는 화면이라 메뉴도 ADMIN 에게만 보인다.

---

## 9. 이번에 만든 것 / 만들지 않은 것

| | 상태 |
|---|---|
| `external_system` / `external_account` (M038) | ✅ |
| `external_entity_mapping` / `sync_run` / `conflict` / `field_authority` (M039) | ✅ |
| 커넥터 계약 + registry + capability | ✅ |
| 동기화 파이프라인 (멱등·권위·충돌·행단위 커밋) | ✅ |
| 관리 화면 (`/integrations/external`) | ✅ |
| 권한 · 감사 · 조직 격리 | ✅ |
| 백엔드 25건 · Vitest 10건 · E2E 8건 | ✅ |
| **실제 셀러툴 커넥터** | ❌ credential 없음 |
| **자격증명 설정 화면** | ❌ vault 연결이 다음 단계 |
| **데이터 품질 검사** (`SYNC_FAILED` 등) | ❌ |
| **Excel 공급처 import** | ❌ 원본 파일 없음 |
| `SYNC_STALE` | ❌ 업무 기준 없음 |

미구현을 "완료" 로 보고하지 않는다.

---

## 10. 다음 Adapter 착수 조건

특정 셀러툴을 자동으로 고르지 않는다. 다음이 **실제로** 준비된 것을 고른다.

1. API 문서
2. credential (테스트 계정)
3. 권한 범위 확인
4. 응답 샘플 캡처 — `test_connection` → `fetch 1 product` 을 read-only 로 실행

4번 없이 매핑을 쓰면 그건 추측이다. 캡처 도구부터 만들고 실제 응답을 본 뒤
`ExternalProduct` DTO 의 필드를 늘린다.
