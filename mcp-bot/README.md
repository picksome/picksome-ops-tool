# mcp-bot

PICKSOME OPS TOOL 판매데이터 자동입력용 MCP 서버.

전체 사용법(계정 발급부터 등록까지)은 **`docs/28_mcp_bot_manual.md`**를 보세요 — 이 README는
디렉토리 요약만 담습니다.

## 구조

- `server.py` — MCP 서버 본체(stdio transport). `list_sale_channels`, `push_sales_orders` 2개 툴 제공.
- `.venv/` — 이 컴포넌트 전용 격리 가상환경. **backend/의 Python 환경과 공유하지 않는다** — `mcp` SDK가
  `starlette`/`httpx` 등 backend가 쓰는 패키지와 버전 충돌을 일으키는 걸 확인해서 분리함.
- `requirements.txt` — `mcp==1.9.4`, `httpx`.

## 빠른 실행 확인

```bash
cd mcp-bot
python -m venv .venv          # 최초 1회
.venv/Scripts/python.exe -m pip install -r requirements.txt
PICKSOME_INTEGRATION_ACCOUNT_ID=... PICKSOME_INTEGRATION_API_KEY=... \
  .venv/Scripts/python.exe server.py
```

계정 발급은 `/automation/integrations` 화면에서 먼저 해야 합니다.
