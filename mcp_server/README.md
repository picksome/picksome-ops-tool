# Seller OS MCP 서버 (선택 모듈)

Claude·OpenClaw 같은 에이전트가 Seller OS 를 **읽고 제안**할 수 있게 하는
어댑터다. Seller OS 의 일부가 아니라 **앞에 붙는 별도 프로세스**다.

```
Claude / OpenClaw ──stdio(MCP)──▶ mcp_server ──HTTP + 토큰──▶ Seller OS
```

## 왜 밖에 있나

의존 방향을 한쪽으로 고정하기 위해서다.

```
MCP  →  Seller OS      허용
Seller OS  →  MCP      금지
```

`backend/app/` 안에서 `mcp` 패키지를 import 하는 곳은 **한 군데도 없다**.
이 디렉터리를 통째로 지워도 주문 수집·마진 감시·정산 대사·일일 리포트는 그대로
돈다(`test_mcp_optional.py` 가 그 사실을 고정한다).

MCP SDK 를 `backend/pyproject.toml` 에 넣지 않은 것도 같은 이유다. Seller OS 를
설치하는 사람이 AI SDK 를 함께 설치할 이유가 없다.

## 설치

**backend 와 같은 Python 환경에 설치하지 않는다.** 별도 venv 를 쓴다.

```bash
cd mcp_server
python -m venv .venv
.venv/Scripts/activate      # Windows
pip install -r requirements.txt
```

## 실행

```bash
export SELLER_OS_URL=http://127.0.0.1:8000
export SELLER_OS_TOKEN=sos_...      # 서비스 계정 토큰
python server.py
```

토큰은 Seller OS 화면에서 발급한다.

```
설정 → AI · API → 서비스 계정 → 토큰 발급
```

발급 화면에서 **한 번만** 보여준다. 서버에는 해시만 남으므로 다시 조회할 수
없다.

## Claude Desktop 연결 예시

```json
{
  "mcpServers": {
    "seller-os": {
      "command": "python",
      "args": ["D:/picksomeprod/picksome-ops-tool/mcp_server/server.py"],
      "env": {
        "SELLER_OS_URL": "http://127.0.0.1:8000",
        "SELLER_OS_TOKEN": "sos_..."
      }
    }
  }
}
```

## 이 서버가 하지 않는 것

- **DB 에 직접 붙지 않는다.** HTTP 로만 이야기한다
- **SQL 을 만들지 않는다**
- **마진·원가를 계산하지 않는다** — 계산된 값을 받아 전달만 한다
- **실행 tool 이 없다.** 읽기와 제안까지다

원가 변경도 `propose_cost_change` 로 **승인 요청**을 만들 뿐이다. 사람이
승인해야 반영된다.

## 권한

tool 목록은 토큰의 scope 에 따라 달라지지만, **목록 필터는 보안이 아니다.**
실제 판정은 Seller OS 가 호출 시점에 한다 — 목록에 없는 tool 을 이름으로
불러도 403 이 돌아온다.

## MCP 가 꺼져 있으면

`MCP_ENABLED=false` 면 Seller OS 가 404 를 돌려준다. 이 서버는 그 사실을
그대로 전한다 — 조용히 빈 목록을 주지 않는다.

## 실접속 스모크

MCP 가 **실제로 붙는지**는 tool 함수를 직접 불러서는 알 수 없다. 클라이언트가
handshake 하고 목록을 받아 호출까지 되어야 한다.

```bash
# 1) Seller OS 를 MCP 켠 채로 띄운다
MCP_ENABLED=true python -m uvicorn app.main:app --port 8155

# 2) 서비스 계정과 토큰을 만든다 (설정 → AI · API 또는 서비스 함수)

# 3) 실제 MCP 클라이언트로 접속한다
python mcp_server/smoke_client.py \
       --url http://127.0.0.1:8155 \
       --token-file <토큰 파일>
```

토큰을 명령행 인자로 받지 않는다 — 프로세스 목록과 셸 히스토리에 남는다.

확인하는 것:

```
initialize          handshake
tools/list          권한에 맞는 목록 + inputSchema
tools/call          실제 호출
scope 거부          목록에 없는 tool 을 이름으로 불러도 막히는가
cost 가림           cost:read 없이 원가가 새지 않는가
```

### 이 스모크가 잡은 실제 결함

Python SDK 의 모델 필드는 **snake_case** 다. camelCase 는 전선 위 alias 일
뿐이라, `inputSchema=` / `isError=` 로 넘기면 **조용히 무시된다.**

```
inputSchema=  →  input_schema=     안 그러면 클라이언트가 인자 없는 tool 을 본다
isError=      →  is_error=         안 그러면 실패가 성공으로 전달된다
```

두 번째가 특히 위험하다 — 권한 거부(403)를 받고도 에이전트가 "됐다" 고
보고하게 된다. 내부 함수 테스트로는 절대 안 잡히고, 실제 클라이언트를
붙여야만 나온다.
