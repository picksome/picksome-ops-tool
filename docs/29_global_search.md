# 29. 통합 검색 (Global Search)

작성: 2026-08-05 / 역할: Planner → Backend → Frontend

## 왜 만드는가

`components/shell/cmd-k-search.tsx` 가 이미 ⌘K 팔레트를 띄우고
`GET /search` 를 호출하고 있는데 **백엔드에 그 라우트가 없다.**
그래서 지금 ⌘K 는 정적 페이지 목록만 필터링하고, 상품은 아무리 쳐도 안 나온다.

동시에 상품 목록의 `q` 필터도 약하다. `name_ko ILIKE '%q%'` 단일 토큰이라
`"쿨링 스카프"` 처럼 **띄어쓰기가 섞인 질의가 실패**한다 — 실제 상품명이
`국산 쿨링 스카프 쿨스카프 얼음스카프` 인데 `쿨링 스카프` 로는 못 찾는다.

## 무엇을 만드는가

### 1) `GET /api/v1/search` — 통합 검색 (신규)

한 번의 질의로 상품 / 판매 / 사업비용을 동시에 찾는다.

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `q` | (필수) | 검색어. 2자 미만이면 빈 결과 |
| `limit` | 5 | **종류별** 최대 건수 (1~20) |

응답 (`envelope` 의 `data`):

```json
{
  "products": [{ "id","business_key","sku","name","status","margin_type" }],
  "sales":    [{ "id","business_key","sale_date","marketplace","name","sale_price" }],
  "expenses": [{ "id","expense_date","name","gross_amount","memo" }],
  "counts":   { "products": 12, "sales": 3, "expenses": 0 }
}
```

`counts` 는 limit 을 걷어낸 전체 건수다 — "12건 중 5건 표시" 를 위해 필요하다.
프론트 기존 타입이 상품명을 `name` 으로 읽으므로 **키 이름을 `name` 으로 맞춘다**
(`name_ko` 가 아니다).

권한: 읽기 권한이 있는 모든 역할(ADMIN/OPERATOR/SOURCING/ACCOUNTING/READONLY).
검색 결과는 **항상 요청자의 org 로 스코프**한다.

### 2) 검색 매칭 규칙 (공통) — `services/search.py`

- **공백으로 토큰 분리 후 AND**. `쿨링 스카프` → `쿨링` AND `스카프`.
  각 토큰은 대상 컬럼 **어디든** 걸리면 된다(컬럼 간에는 OR).
- 대소문자 무시(`ILIKE`), 앞뒤 공백 제거, 2자 미만 질의는 빈 결과.
- `%` `_` 는 LIKE 와일드카드라 **이스케이프**한다. 안 하면 `%` 한 글자가
  전체 매칭이 되어 아무 의미 없는 결과가 쏟아진다.
- 최대 토큰 8개. 그 이상은 잘라낸다(질의 폭주 방지).

대상 컬럼:

| 종류 | 컬럼 |
|---|---|
| 상품 | `name_ko`, `sku`, `business_key`, `category_path` |
| 판매 | `product_snapshot_name`, `business_key`, `marketplace_order_no` |
| 사업비용 | `description`, `memo` |

### 3) 정렬 — 관련도 + 안정성

`ORDER BY rank, <시간> DESC, id DESC`

`rank` 는 첫 토큰 기준 3단계:
0 = 완전일치, 1 = 앞부분 일치(prefix), 2 = 부분 일치.
사용자가 `PICK0294` 를 치면 그 상품이 1번으로 올라온다.

**`id` 타이브레이커가 반드시 들어가야 한다.** 아래 버그 참조.

### 4) 상품 목록 `q` 도 같은 규칙으로 교체

`services/product.py::list_products` 의 `q` 처리를 위 공통 헬퍼로 바꾼다.
검색창이 두 개(목록 필터 / ⌘K)인데 규칙이 다르면 사용자가 혼란스럽다.

## 🔴 같이 고치는 선행 버그

이 두 가지를 안 고치면 검색 결과가 화면에 안 뜨거나 어긋난다.

### (a) 목록 응답 envelope 이 화면에 도달하지 않는다

`lib/api.ts` 가 `return body?.data ?? body` 로 **meta 를 통째로 버렸다.**
목록 API 는 `{ data: [...], meta: { total } }` 인데 화면은 `{ items, total }`
을 읽으므로 `data.items === undefined` → **모든 목록 화면이 영구 빈 상태**였다.
상품 1,065건이 있는데도 "등록된 상품이 없습니다" 가 떴다.

→ 배열 + meta 인 응답에 한해 `{ ...meta, items, total }` 로 합쳐서 반환.

### (b) 페이지네이션 파라미터가 절반의 엔드포인트에서 무시된다

BE 가 두 가지를 섞어 쓴다:

- `limit` / `offset` — products, expenses, policies, notifications, admin_*
- `page` / `page_size` — sales, sourcing, reports, marketplace_settlements

FE 는 전부 `page`/`page_size` 로 보내서, `limit/offset` 쪽은 항상 1페이지만 나왔다.

→ `buildUrl` 이 `page`/`page_size` 를 받으면 `limit`/`offset` 도 함께 실어 보낸다.
   FastAPI 는 모르는 쿼리를 무시하므로 어느 쪽을 읽든 같은 페이지가 나온다.

### (c) 정렬 키가 유일하지 않아 페이지가 겹친다

`list_products` 가 `ORDER BY created_at DESC` 인데, 엑셀 import 로 들어온
1,065건은 **created_at 이 전부 동일**하다(한 트랜잭션). 정렬이 비결정적이라
1페이지와 2페이지에 **같은 행이 중복으로 나오고 어떤 행은 아예 안 나온다.**
실제로 `offset=0` 과 `offset=2` 의 첫 행이 같은 `PROD-000880` 이었다.

→ 모든 목록/검색 정렬에 `id` 타이브레이커를 붙인다.

## 하지 않는 것 (범위 밖)

- **형태소 분석 / 유사어 / 오타 교정.** 한국어 형태소 분석기는 별도 인프라가 필요하다.
  현재 데이터 규모(상품 1,065 / 판매 374)에서는 ILIKE AND 로 충분하다.
- **전문검색 인덱스(GIN/pg_trgm).** 지금 규모에선 순차 스캔이 수 ms 다.
  10만 건을 넘기면 `pg_trgm` + GIN 을 얹는다 — 그때 인덱스만 추가하면 되도록
  매칭 로직을 `services/search.py` 한 곳에 모아둔다.
- 검색 결과 전용 페이지(`/search?q=`). ⌘K 팔레트로 먼저 검증하고 필요하면 추가.

## 체크리스트

- [x] `services/search.py` — 토큰화 + 이스케이프 + WHERE/rank 빌더
- [x] `api/v1/search.py` — `GET /search`, v1 라우터 등록
- [x] `list_products` q 를 공통 헬퍼로 교체 + `id` 타이브레이커
- [x] `lib/api.ts` envelope meta 병합 + page↔limit 변환
- [x] ⌘K 팔레트에 판매/사업비용 그룹 추가 + 건수 표기
- [x] 단위테스트 (토큰화·이스케이프·빈질의)
