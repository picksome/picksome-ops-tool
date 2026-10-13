# 28. 판매데이터 자동입력 봇/MCP 매뉴얼

**대상 독자**: 이 문서는 사람이 아니라 **AI 에이전트/봇이 읽고 그대로 따라 실행**할 수 있도록 쓴다.
모호한 표현 대신 정확한 명령어·경로·JSON 스키마를 그대로 적는다. 사람이 읽어도 무방하다.

**목적**: 마켓플레이스(쿠팡 등)에서 발생한 판매 데이터를 PICKSOME OPS TOOL에 자동으로 넣는다.
설계 배경은 `docs/27_dynamic_channel_and_bot_integration_plan.md` 3-3 (투트랙 중 "봇 push" 트랙).

**전체 그림**: `봇/MCP 클라이언트 → (HTTPS + API Key) → PICKSOME 백엔드 → sales_order 테이블`

---

## 0. 지금 상태 요약 (2026-07-29 기준)

- 백엔드 API: 완성, 실사용 가능
- 웹 화면(봇 계정 발급): `/automation/integrations` — 완성
- MCP 서버(`mcp-bot/server.py`): 완성, stdio 프로토콜로 실제 검증 완료
- 마켓 API 직접 연동(트랙 A, 이 문서가 다루는 봇 push 트랙과 다른 트랙): **미착수**

---

## 1. 사전 준비 — 봇 계정 발급

봇이 데이터를 넣으려면 먼저 **연동 계정(integration_account) + API Key**가 있어야 한다. 발급은 사람이 로그인한 상태에서 한 번만 하면 된다.

### 1-1. 웹 화면에서 발급 (권장)

1. `http://localhost:3000` 접속 후 로그인 (ADMIN 또는 OPERATOR 권한 필요)
2. 왼쪽 사이드바 **Automation → 봇/API 연동 계정** (`/automation/integrations`)
3. **"새 봇 계정 발급"** 클릭
4. 이름(예: "쿠팡 자동입력 봇"), 종류(예: `MCP_CLAUDE`), 판매채널(선택, 예: `COUPANG`) 입력 후 발급
5. 화면에 뜨는 **계정 ID(account_id)**와 **API Key**를 즉시 복사해 안전한 곳(비밀번호 관리자 등)에 저장한다 — **API Key는 이 화면에서 딱 한 번만 보인다.** 새로고침하면 다시 못 본다(DB에는 해시만 저장되기 때문, 평문 미저장 원칙).

### 1-2. API로 직접 발급 (사람의 JWT 토큰 필요)

```bash
# 1) 로그인해서 JWT 받기
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "관리자이메일", "password": "비밀번호"}'
# 응답의 data.access_token 을 아래에 사용

# 2) 봇 계정 발급
curl -s -X POST http://localhost:8000/api/v1/integrations \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "MCP_CLAUDE",
    "account_label": "쿠팡 자동입력 봇",
    "sale_channel_code": "COUPANG"
  }'
```

응답:
```json
{
  "data": {
    "id": "3c9c7e69-047e-466a-8b35-07f48c7ee69f",
    "provider": "MCP_CLAUDE",
    "account_label": "쿠팡 자동입력 봇",
    "api_key": "<발급된 API Key — 이 응답에서만 1회 표시>"
  }
}
```

`data.id` = account_id, `data.api_key` = API Key. **이 응답이 유일하게 키를 보여주는 순간이다.**

---

## 2. MCP 서버 설정 (Claude 같은 MCP 클라이언트에서 쓰는 경우)

### 2-1. 준비물

- Python 3.10+ (mcp-bot 은 이미 격리된 venv 를 씀 — 별도 설치 불필요)
- 1장에서 받은 `account_id`, `api_key`

### 2-2. venv 확인/재설치

이미 만들어져 있다: `mcp-bot/.venv`. 없다면:
```bash
cd mcp-bot
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

### 2-3. Claude Code(.mcp.json)에 등록

프로젝트 루트 또는 사용자 설정에 `.mcp.json`:
```json
{
  "mcpServers": {
    "picksome-ops-bot": {
      "command": "D:\\picksomeprod\\picksome-ops-tool\\mcp-bot\\.venv\\Scripts\\python.exe",
      "args": ["D:\\picksomeprod\\picksome-ops-tool\\mcp-bot\\server.py"],
      "env": {
        "PICKSOME_API_BASE": "http://localhost:8000/api/v1",
        "PICKSOME_INTEGRATION_ACCOUNT_ID": "<1장에서 받은 account_id>",
        "PICKSOME_INTEGRATION_API_KEY": "<1장에서 받은 api_key>"
      }
    }
  }
}
```

### 2-4. Claude Desktop(claude_desktop_config.json)에 등록

경로 예: `%APPDATA%\Claude\claude_desktop_config.json` — 형식은 위 2-3과 동일한 `mcpServers` 블록을 그대로 쓴다.

### 2-5. 등록 후 확인

MCP 클라이언트를 재시작하면 툴 목록에 `list_sale_channels`, `push_sales_orders` 두 개가 보여야 한다.

---

## 3. 툴 스펙

### 3-1. `list_sale_channels()`

인자 없음. 이 조직에서 유효한 판매채널(마켓) 코드 목록을 반환한다.

**항상 `push_sales_orders` 호출 전에 먼저 이걸로 marketplace 코드를 확인할 것** — 시스템 기본 9종(COUPANG/SMARTSTORE/ELEVENST/GMARKET/AUCTION/SELF_MALL/OTHER/TEMU/TOSS) 외에 조직이 직접 추가한 커스텀 채널(예: OHOUSE)이 있을 수 있다.

응답 예:
```json
[
  {"code": "COUPANG", "display_name": "쿠팡"},
  {"code": "OHOUSE", "display_name": "오늘의집"}
]
```

### 3-2. `push_sales_orders(request)`

**입력**: `request.items` — 1~500건의 주문 배열. 각 항목:

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `sale_date` | string | ✅ | `"YYYY-MM-DD"` |
| `marketplace` | string | ✅ | `list_sale_channels` 로 확인한 정확한 코드 |
| `marketplace_order_no` | string | ✅ | 마켓 주문번호. 같은 (마켓, 주문번호) 재전송은 안전하게 실패 처리(중복 적재 안 됨) |
| `sku` | string \| null | | 상품 매칭용 — 있으면 우선 사용 |
| `product_business_key` | string \| null | | sku 매칭 실패 시 대안 (예: `"PROD-000001"`) |
| `product_snapshot_name` | string \| null | | 상품명 (매칭 실패해도 기록은 됨) |
| `quantity` | int | ✅ | 1 이상 |
| `sale_price` | int | ✅ | **원 단위 정수(KRW). 소수/float 금지.** 0 이상 |
| `ad_cost` | int | | 기본 0 |
| `shipping_fee` | int | | 기본 0 |
| `memo` | string \| null | | |

**호출 예**:
```json
{
  "request": {
    "items": [
      {
        "sale_date": "2026-07-29",
        "marketplace": "COUPANG",
        "marketplace_order_no": "C2026072912345",
        "sku": "MYSHOP-001",
        "quantity": 2,
        "sale_price": 15000,
        "ad_cost": 1200
      }
    ]
  }
}
```

**응답**:
```json
{
  "batch_run_id": "05a5ddec-1434-4c86-8ba9-d58a739cfa6c",
  "status": "SUCCESS",
  "success_count": 1,
  "failed_count": 0
}
```

`status` 는 `SUCCESS` / `PARTIAL` / `FAILED` 중 하나. **`PARTIAL`이나 `FAILED`가 나와도 예외가 아니다** — 배치 안에서 일부만 실패했다는 뜻이고, 성공한 항목은 이미 반영됐다. 실패 이유는 이 응답에 포함되지 않는다(요약치만 옴) — 상세 원인이 필요하면 사람이 `/automation/integrations`나 DB의 `batch_run_log` 테이블(`batch_run_id` 로 조회)을 확인해야 한다.

---

## 4. 흔한 실패 원인과 대응

| 증상 | 원인 | 대응 |
|---|---|---|
| 툴 호출 자체가 에러(`RuntimeError`) | API Key/계정 ID가 틀렸거나 계정이 비활성 | 1장부터 재발급 |
| `failed_count > 0`, 특정 건만 | `marketplace` 코드 오타/미등록 | `list_sale_channels` 로 정확한 코드 재확인, 필요시 `/automation/integrations`에서 판매채널 관리 화면(`/policies/fees`)에서 새 채널 추가 |
| `failed_count > 0`, 재전송 시 | 같은 (marketplace, marketplace_order_no) 이미 등록됨 | 정상 동작 — 중복 방지가 의도대로 작동한 것. 데이터 유실 아님 |
| `sale_price`에 소수를 넣음 | 스키마 위반 | 정수(원 단위)로 반올림해서 보낼 것 |
| 상품이 매칭 안 됨(product_id 비어서 등록됨) | `sku`/`product_business_key` 가 실제 상품과 안 맞음 | 판매 장부 화면(`/sales`)에서 수동으로 상품 연결 필요 — 자동 매칭 실패는 등록 자체를 막지 않음(product_id 없이도 저장됨) |

---

## 5. MCP 없이 REST API로 직접 호출하는 법

MCP 클라이언트가 아니라 임의의 스크립트/봇이라면 MCP 프로토콜 없이 그냥 HTTP로 호출해도 된다 — MCP 서버는 이 API의 얇은 래퍼일 뿐이다.

```bash
# 유효 채널 조회
curl -s http://localhost:8000/api/v1/integrations/<ACCOUNT_ID>/sale-channels \
  -H "X-Integration-Key: <API_KEY>"

# 판매 데이터 등록
curl -s -X POST http://localhost:8000/api/v1/integrations/<ACCOUNT_ID>/ingest/orders \
  -H "X-Integration-Key: <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"sale_date": "2026-07-29", "marketplace": "COUPANG",
       "marketplace_order_no": "C123", "quantity": 1, "sale_price": 15000}
    ]
  }'
```

인증 헤더가 `Authorization: Bearer`가 아니라 **`X-Integration-Key`**라는 점에 주의 — 이 두 엔드포인트(`/sale-channels`, `/ingest/orders`)만 이 헤더를 쓰고, 나머지 API(`/sale-channels` 전체 목록 등 사람용 화면 API)는 JWT를 쓴다.

---

## 6. 관련 문서

- `docs/27_dynamic_channel_and_bot_integration_plan.md` — 전체 설계(D-287~D-291), 이 문서가 다루는 "봇 push" 트랙 외에 "API pull" 트랙(마켓 API 직접 연동)도 설명
- `backend/app/api/v1/integration.py` — 실제 라우터 코드
- `mcp-bot/server.py` — MCP 서버 구현
