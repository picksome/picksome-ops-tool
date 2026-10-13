# 27. 판매채널 동적 확장 + 환율 자동화 + 투트랙 연동(API/봇) 기획

**작성**: 2026-07-29. Role: PM/Architect + Planner + DBA 통합 초안 → 같은 세션에서 1~3단계 Backend/DBA 구현까지 완료.
**상태**: 1~3단계 코드 + 마이그레이션(M007~M009) 전부 작성 완료, 로컬 dev DB(picksome_ops) 반영 + 트랜잭션 롤백 방식으로 end-to-end 검증 완료. **Frontend 연동도 완료**(아래 5절 표 참고) — `tsc --noEmit` 통과.

## 1. 요청 요약

1. 환율을 수동 입력이 아니라 **자동(실시간 API)**으로.
2. 판매채널(마켓)을 **사용자가 직접 추가**할 수 있게 (예: 오늘의집). 지금은 Postgres ENUM이라 코드 배포 없이는 못 늘림.
3. 마켓 **API로 판매금액 등을 가져오는** 기능.
4. 마켓별 **약간의 커스텀 요소**(필드/설정)가 들어갈 여지.
5. 나중에 **자동화 봇**이 API처럼 데이터를 밀어넣을 수 있는 구조 — **API 직접 연동**과 **봇 자동입력**의 **투트랙**.

기존에 `CLAUDE.md`의 External Integration Roadmap과 `docs/db_schema/{integration_account,integration_job,batch_run,batch_run_log,marketplace_order_import,marketplace_settlement_import,external_product_mapping,supplier_price_snapshot}.yaml`에 **이걸 위한 뼈대가 이미 Post-MVP로 설계**돼 있었음. 이번 기획은 그 뼈대를 "지금 착수"로 승격 + 판매채널 동적화 + 봇 트랙을 추가하는 확장.

## 2. 결정 필요 사항 (사용자 확인 후 착수)

| # | 결정 사항 | 권장안 | 대안 |
|---|---|---|---|
| D-287 | 판매채널을 ENUM에서 어떻게 뺄까 | **ENUM → TEXT 컬럼 전환 + `sale_channel` 카탈로그 테이블로 검증**. DB 레벨 제약(ENUM)을 앱 레벨 검증(FK성 참조)으로 이동 | ENUM 유지 + "OTHER 하나로 뭉뚱그리고 표시이름만 별도 테이블" (제약: 마켓별 수수료/계산 로직이 OTHER로 뭉쳐서 정확도 하락 — 비권장) |
| D-288 | 환율 소스 | **한국수출입은행 Open API**(무료, 일 1회 고시환율) — 매일 배치로 `exchange_rate_snapshot` 적재 | 유료 API(실시간 틱 단위) — 사입/구매대행은 하루 단위 결제가 대부분이라 일 1회로 충분, 비용 대비 과함 |
| D-289 | 봇 인증 방식 | **API Key 발급형**(vault에 해시 저장, 요청 헤더로 전달) — MCP/봇이 이 키로 ingest 엔드포인트 호출 | OAuth2 — 봇 하나당 계정 흐름 만들 필요 없어 MVP엔 과함 |
| D-290 | 마켓별 커스텀 필드 범위 | **MVP는 "연동 설정"(API 엔드포인트/인증정보/파서 종류)만 커스텀.** 마켓별 필드 매핑 DSL(사용자가 필드명 자유 매핑)은 Post-MVP | 완전 자유 매핑 DSL — 초기부터 만들면 과설계 위험, 실사용 패턴 보고 나중에 결정 |
| D-291 | API 연동과 봇 연동이 최종 데이터에 미치는 신뢰도 표시 | 정책처럼 `reliability_status`/`source_type`을 `marketplace_order_import`/`marketplace_settlement_import`에도 부여(API_AUTO / BOT_AUTO / MANUAL) — 나중에 배분 오차 추적 시 출처 구분 필요 | 구분 없음 (비권장 — 봇 오입력과 공식 API 값을 나중에 못 가름) |

## 3. 데이터 모델 확장안

### 3-1. 판매채널 동적화 (`sale_channel` 신설)

```
sale_channel
  id UUID PK
  org_id UUID FK (NULL = SYSTEM 기본 카탈로그, org 는 자기 것만 추가 가능)
  code TEXT NOT NULL        -- 'COUPANG' 'TEMU' 'OHOUSE' 등, org 내 unique
  display_name TEXT NOT NULL -- '오늘의집'
  is_system_default BOOLEAN  -- SYSTEM 시드(코드 초기 7종)면 TRUE, 사용자 추가면 FALSE
  integration_kind TEXT NULL -- NONE / API / BOT (연동 가능 여부, 3-3 참고)
  is_active BOOLEAN
  + audit_columns
```

- 기존 6개 테이블(`marketplace_fee_policy`, `ad_cost_policy`, `return_risk_policy`, `product_margin_expected`, `sales_order`, `marketplace_settlement_record`)의 `marketplace enum_sale_channel` 컬럼을 **`marketplace TEXT`로 변경**, 값은 `sale_channel.code` 참조(앱 레벨 검증 — DB FK는 대소문자/신구값 이관 리스크 때문에 강제하지 않고 서비스 레이어에서 `resolve_sale_channel()`로 검증).
- 기존 COUPANG/SMARTSTORE/ELEVENST/GMARKET/AUCTION/SELF_MALL/OTHER/TEMU/TOSS 9종은 `sale_channel`에 `is_system_default=TRUE`로 시드 — **값 자체는 안 바뀌므로 기존 데이터 무손상**.
- 사용자가 "오늘의집 추가"하면 그냥 `sale_channel` INSERT 1건 — 마이그레이션/배포 불필요. `marketplace_fee_policy`도 그 code로 정책 row를 만들면 바로 계산에 반영됨(기존 정책 조회 로직 `resolve_active_policies`는 `marketplace` 값을 문자열 비교로 쓰고 있어 ENUM 여부와 무관하게 그대로 동작).

### 3-2. 환율 자동화

```
exchange_rate_snapshot
  id UUID PK
  currency TEXT NOT NULL       -- 'USD' 'CNY' 'JPY' 등
  rate_to_krw NUMERIC(12,4) NOT NULL
  rate_date DATE NOT NULL      -- 고시 기준일
  source TEXT NOT NULL         -- 'KOREAEXIM_API' / 'MANUAL'
  fetched_at TIMESTAMPTZ NOT NULL
  UNIQUE(currency, rate_date)
```

- 배치(`integration_job.job_kind = 'FX_RATE_SYNC'`, 매일 1회)가 한국수출입은행 API를 호출해 그날 고시환율을 적재.
- `product.cost_fx_rate` 입력 시 **UI가 그날 `exchange_rate_snapshot` 최신값을 기본 채움** — 정책 자동값과 동일한 패턴(회색 배경 + i, 수동 override 시 노란 배경). `cost_amount_foreign × cost_fx_rate` 자동환산 로직(M005, `services/product.py`)은 그대로 재사용, "값을 누가 채우느냐"만 자동화.
- `product_margin_actual` 계산 시점엔 **매입 당시(product 원가 입력 시점)에 박제된 `cost_fx_rate`를 그대로 사용** — 정산 시점 환율로 재계산하지 않음(정책 박제 원칙과 동일 논리, 과거 재현 가능해야 함).

### 3-3. 투트랙 연동 파이프라인 (API 직접 연동 / 봇 자동입력)

핵심 설계: **두 트랙이 같은 파이프라인으로 수렴한다.** API가 데이터를 끌어오든(pull), 봇이 데이터를 밀어넣든(push), 둘 다 동일하게 `batch_run` 1건 + `marketplace_order_import`/`marketplace_settlement_import`(raw JSON 박제) → 정규화 함수가 `sales_order`/`marketplace_settlement_record`로 변환하는 흐름.

```
integration_account
  provider TEXT           -- 'COUPANG_WING_API' | 'BOT_GENERIC' | ...
  sale_channel_id UUID FK -- 3-1 의 sale_channel 참조 (신규 FK 추가)
  auth_kind TEXT          -- 'API_KEY' | 'OAUTH' | 'BOT_TOKEN' (신규 컬럼)
  config_json JSONB NULL  -- 마켓별 커스텀 설정(엔드포인트 URL, 페이지네이션 방식 등) — D-290
  credentials_vault_entry_id UUID  -- 기존 그대로, 실 값은 vault, DB엔 참조만

integration_job
  job_kind: 'ORDER_SYNC' | 'SETTLEMENT_SYNC' | 'PRICE_CRAWL' | 'FX_RATE_SYNC'  (기존 + FX 추가)
  trigger_kind TEXT       -- 'SCHEDULE'(API 폴링) | 'WEBHOOK'(봇 push) — 신규 컬럼
```

**트랙 A — API 직접 연동(pull)**: `integration_job`이 cron으로 마켓 API를 호출 → 응답을 `marketplace_order_import.raw_json`에 박제 → 정규화 서비스가 `sales_order` INSERT.

**트랙 B — 봇 자동입력(push)**: 신규 엔드포인트
```
POST /integrations/{account_id}/ingest/orders
POST /integrations/{account_id}/ingest/settlements
Header: X-Integration-Key: <API Key>
Body: [ { raw order/settlement json }, ... ]
```
봇(또는 MCP 툴)이 이 키로 호출하면 서버가 `integration_account`의 키를 검증 → `batch_run` 생성 → 각 항목을 `marketplace_order_import`(또는 settlement)에 박제 → **트랙 A와 동일한 정규화 함수를 재사용**해서 `sales_order`로 변환. 즉 정규화 로직은 마켓별 파서 1벌만 만들면 API pull이든 봇 push든 똑같이 탄다.

- 마켓별 파서(raw_json → sales_order 필드 매핑)는 MVP에서 **마켓 코드별 명명된 Python 함수**로 시작(`parsers/coupang.py`, `parsers/ohouse.py` 등) — D-290에 따라 매핑 DSL은 미룸. 새 마켓(오늘의집 등) 추가 시 `sale_channel` row + 파서 함수 1개 + `integration_account` 설정만 있으면 됨(스키마 변경 불필요).
- 정규화 실패/중복 등은 `batch_run_log`에 기록, `batch_run.status`로 성공/부분성공/실패 노출 — 기존 설계 그대로 재사용.

## 4. MVP 범위 제안

**이번 착수분(승인 시 바로 코드화 가능한 범위)**:
- `sale_channel` 테이블 + 기존 9종 시드 + 마이그레이션(ENUM → TEXT, 기존 6테이블)
- `exchange_rate_snapshot` 테이블 + 수동 시드 1회분(배치 잡 자체는 실제 크론 인프라 없는 로컬 환경 특성상 "수동 트리거 가능한 함수"로 우선 구현, 실제 스케줄러는 DevOps 역할에서 별도)
- `integration_account`/`integration_job`/`batch_run`/`batch_run_log`/`marketplace_order_import`/`marketplace_settlement_import` — 기존 스키마 그대로 실제 테이블로 생성(지금까지 문서에만 있었고 M001에 테이블 자체가 있는지부터 재확인 필요)
- 봇 ingest 엔드포인트 2개 + API Key 발급/검증
- 마켓 파서 1개(기존 COUPANG CSV import 로직 재사용 가능한지 확인 후 결정)

**Post-MVP로 남기는 것**: 실시간 크론 스케줄러 자체(현재 로컬 개발 환경은 Docker 없이 standalone — 스케줄러 인프라 자체가 별도 DevOps 결정 필요), 필드 매핑 DSL, OAuth 연동.

## 5. 구현 결과 (2026-07-29, Backend/DBA)

실제 구현은 3장 설계에서 한 가지 단순화가 있었다 — **마켓별 파서(raw_json → sales_order 매핑) 대신, 봇/호출자가 이미 우리 스키마(`IngestOrderItem`: sale_date/marketplace/marketplace_order_no/sku/quantity/sale_price/...)에 맞춰 보내는 "캐노니컬 envelope" 방식**으로 갔다. 마켓별 원본 포맷 파싱은 봇/어댑터 쪽 책임으로 넘기고, 서버는 형식이 통일된 입력만 받는다 — MVP 범위에서 마켓별 파서 함수를 따로 안 만들어도 되는 대신, "봇이 원본을 우리 포맷으로 변환해야 한다"는 제약이 생김(Post-MVP: 필요해지면 마켓별 원본 파서를 추가해 이 변환을 서버가 대신하게 확장 가능).

| 계획 | 실제 | 위치 |
|---|---|---|
| `sale_channel` 테이블 + ENUM→TEXT (M007) | 완료. 9종 시드 + org 커스텀 추가 확인(오늘의집 시나리오로 검증) | `M007_sale_channel_dynamic.py`, `services/sale_channel.py` |
| `GET/POST /sale-channels` | 완료 | `api/v1/sale_channel.py` |
| `exchange_rate_snapshot` + 자동조회 (M008) | 완료. 한국수출입은행 API 연동 코드는 실제 엔드포인트로 스키마 검증까지 마침(키 없이는 파싱 경로만 확인). `KOREAEXIM_API_KEY` 미설정 시 `/sync` 는 명확히 막히고 `/manual` 로 fallback | `M008_exchange_rate_snapshot.py`, `services/exchange_rate.py` |
| `product.cost_fx_rate` 자동 채움 | 완료. 사용자가 직접 입력하면 override, 안 하면 최신 스냅샷 자동 채움 → 그 값이 그대로 박제 | `services/product.py::_resolve_fx_rate` |
| `integration_account` 확장 (M009) | 완료. `sale_channel_id`/`auth_kind`/`config_json`/`api_key_hash` 추가, `credentials_vault_entry_id` nullable 화 | `M009_integration_bot_ingest.py` |
| 봇 ingest 엔드포인트 | 완료. `POST /integrations`(계정발급, JWT) + `POST /integrations/{id}/ingest/orders`(X-Integration-Key, 캐노니컬 envelope) | `api/v1/integration.py`, `services/integration_ingest.py` |
| 트랙 A(API pull, cron) | **미착수** — `integration_job.trigger_kind='SCHEDULE'` 컬럼만 준비. 실제 스케줄러 인프라(Docker 없는 로컬 환경 특성상 별도 DevOps 결정 필요)는 여전히 Post-MVP |
| 마켓별 원본 파서(`parsers/coupang.py` 등) | **미착수** — 위 단순화로 인해 이번 범위에서 불필요해짐. 실제 마켓 원본 포맷 자동 변환이 필요해지면 그때 추가 |
| Frontend 연동 | **완료(2026-07-29 이어서)**. `hooks/useSaleChannels.ts`(GET /sale-channels) 신설, 7개 페이지(`margin/calculator`, `margin/simulator`, `policies/fees`, `sales/import`, `sales`, `settlements/marketplace`, `products/[id]`) + `lib/api/{margin,policies,sales}.ts` 전부 하드코딩 제거. `policies/fees`에 "새 채널 추가" 인라인 폼 추가(오늘의집 등 org가 직접 등록). `tsc --noEmit` 신규 에러 0건(기존 2건 그대로) |
| 환율 관리 화면 | **완료(2026-07-29 더 이어서)**. `/policies/exchange-rates` — USD/CNY/JPY 카드 + "한국수출입은행 API로 오늘 환율 조회" 버튼(실제 자동조회 트리거) + 수동 입력 fallback |
| 봇 계정 발급 화면 | **완료**. `/automation/integrations` — 계정 생성 시 API Key 1회 노출 + 목록. 백엔드에 `GET /integrations`(목록), `GET /integrations/{id}/sale-channels`(봇 전용, X-Integration-Key 인증) 신규 추가 |
| MCP 서버 | **완료**. `mcp-bot/server.py` — `list_sale_channels`/`push_sales_orders` 2개 툴. **격리된 전용 venv 사용** — 처음에 backend 글로벌 Python 환경에 `mcp` SDK를 설치했다가 `starlette`가 0.46.2→1.3.1로 강제 업그레이드되며 fastapi/aider-chat이 깨지는 사고가 있었음(바로 롤백함) — 이후 `mcp-bot/.venv/`로 완전 분리. 실제 stdio MCP 프로토콜로 전체 흐름(계정 발급→list_sale_channels→push_sales_orders→sales_order 반영) 검증 완료, 테스트 데이터는 정리함 |
| 매뉴얼 | **완료**. `docs/28_mcp_bot_manual.md` — AI 에이전트가 그대로 따라할 수 있게 정확한 명령어/JSON 스키마로 작성 |

모든 신규 마이그레이션(M007~M009)은 로컬 dev DB(`picksome_ops`)에 적용 완료, 각 단계 SAVEPOINT 트랜잭션으로 격리한 스크립트로 end-to-end 동작 검증(성공 케이스 + 실패 케이스 + 롤백 후 DB 무결성) 완료. 통합테스트 파일 3개 추가(`test_sale_channel_integration.py`, `test_exchange_rate_integration.py`, `test_integration_ingest.py`) — `picksome` DB 롤에 CREATEDB 권한이 없어 `pytest` 로는 아직 못 돌림(기존에 알려진 인프라 이슈, [[project_local_dev_setup]]).

## 6. Open Questions (남은 부분)

- D-287~D-291 권장안대로 진행해도 되는지
- ~~`integration_account` 등 8개 테이블이 실제 M001에 테이블로 생성돼 있는지~~ → **확인 완료(2026-07-29)**: `batch_run`/`batch_run_log`/`integration_account`/`integration_job`/`external_product_mapping`/`supplier_price_snapshot`/`marketplace_order_import`/`marketplace_settlement_import` **8개 전부 M001에 이미 테이블로 존재**(`backend/app/db/migrations/versions/M001_initial_schema.py:590,957-1019`). 다만 지금까지 org UUID 등 컬럼만 있고 `sale_channel_id`/`auth_kind`/`config_json`(3-3 신규 컬럼), API/봇 ingest 엔드포인트, 정규화 서비스 로직은 전부 미작성 — 순수 골격만 있는 상태.
