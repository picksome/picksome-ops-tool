# 30. 백엔드 기술 스택 (BE)

> PICKSOME OPS TOOL 백엔드에서 사용하는 기술과 채택 이유.
> 작성일 2026-06-22 / 작성자 Backend Role.

---

## 1. 핵심 스택

| 분류 | 기술 | 버전 | 채택 이유 |
|------|------|------|-----------|
| 언어 | Python | 3.12+ | 정적 타입 힌트(PEP 695), 향상된 generic, `slots` dataclass |
| 웹 프레임워크 | **FastAPI** | 0.115+ | async 기본, OpenAPI 자동 생성, pydantic v2 통합, 의존성 주입 표준 |
| ASGI 서버 | uvicorn[standard] | 0.32+ | uvloop + httptools 빠름. FastAPI 권장 조합 |
| ORM/SQL | **SQLAlchemy 2** | 2.0.36+ | text() raw SQL 과 ORM 양쪽 가능. 핵심 계산은 raw SQL 로 의도 명시, CRUD 는 ORM |
| DB 드라이버 | psycopg | v3.2+ (binary) | psycopg2 대비 async 지원, COPY/binary 효율 |
| 마이그레이션 | **Alembic** | 1.14+ | 손으로 쓰는 SQL 마이그레이션 — autogenerate 미사용 (의도된 보수성) |
| 검증/스키마 | **pydantic v2** | 2.9+ | 도메인 객체 단일화. 라우터/서비스/도메인 동일 모델 흐름 |
| 설정 | pydantic-settings | 2.6+ | env + .env 일원화, 타입 안전 |

---

## 2. 보안 / 인증

| 분류 | 기술 | 채택 이유 |
|------|------|-----------|
| 비밀번호 해시 | **argon2-cffi** | bcrypt 대비 GPU/ASIC 저항성. memory-hard. argon2id 모드 권장(OWASP) |
| 토큰 | python-jose[cryptography] | JWT HS256. stateless access/refresh 분리 |
| 평문 비밀번호 차단 | 자체 regex (`services/vault.py`) | CLAUDE.md Security Rule: vault 에 평문 저장 금지 → 입력 단계에서 패턴 매칭 후 `MaskedSecretDetectedError` |
| 권한 | `core/rbac.py` `require_roles()` | 라우터 의존성으로 6역할 RBAC. DB row 단의 org_id 필터 별도 적용 (멀티테넌트 격리) |

---

## 3. ID 생성 (D-283)

| 종류 | 도구 | 이유 |
|------|------|------|
| 모든 PK/FK | **UUID v7** (`uuid7` 라이브러리) | 시간순 정렬 가능 → B-tree 인덱스 친화. v4 대비 hot-page 갱신 효율 |
| DB default fallback | `gen_random_uuid()` (pgcrypto) | Postgres native 가 v7 미지원. 앱이 죽어도 row 삽입은 v4 fallback |
| 사용자 노출 ID | `business_key` (별도 컬럼) | UUID 노출 금지 — `SETTLE-2026-06`, `PROD-00012` 등 인간 친화 키 |

---

## 4. 표준 응답 / 예외

`core/response.py`:

```python
{ "data": ..., "meta": { ... }, "error": null }
```

- `DomainError` 계층 (`core/exceptions.py`) → 4xx 비즈니스 오류 매핑
- `RequestValidationError` → 422 + 필드별 에러
- 모든 mutation 은 `record_audit()` + `db.commit()` 책임 (service 레이어)

---

## 5. 알림 (D-281)

`core/notification/`:

- **ChannelAdapter Protocol** — `send(payload) -> bool` 단일 메서드
- **MVP 채널**: `InAppChannel`, `EmailSmtpChannel`
- **Post-MVP**: KAKAO/SLACK/TELEGRAM/WEBHOOK — feature flag 로 비활성, 새 채널은 `register_channel()` 한 번이면 확장 완료
- 환경 변수 의존성은 `notification_channel.requires_config_json` 에서 명시

---

## 6. Excel / CSV 입출력

| 용도 | 라이브러리 | 이유 |
|------|-----------|------|
| Excel write | **xlsxwriter** 3.2+ | 한국어 컬럼명 안전, 메모리 효율적 (스트리밍 가능) |
| Excel read (import) | **openpyxl** 3.1+ | 셀 단위 조회 + 데이터 검증 친화 |
| CSV write | stdlib `csv` + UTF-8 BOM | Excel 호환. 한국어 깨짐 방지 |

---

## 7. 외부 통합 (Post-MVP 대비)

| 라이브러리 | 용도 |
|-----------|------|
| **httpx** 0.28+ | async HTTP. 쿠팡/스마트스토어 API, 채널 어댑터 호출 |
| python-multipart | 향후 증빙 파일 업로드 |
| email-validator | invite 시 이메일 RFC 검증 |

---

## 8. 개발 도구

| 도구 | 용도 |
|------|------|
| **ruff** 0.8+ | lint + format. line-length 100. select=E,F,I,B,UP,N |
| **mypy** 1.13+ | strict 모드. pydantic.mypy 플러그인 |
| **pytest** 8.3+ | 단위/통합 테스트. asyncio_mode=auto |
| pytest-asyncio / pytest-cov | async 테스트 + 커버리지 |

---

## 9. 채택하지 않은 기술 (의도적)

| 선택지 | 채택 안 함 이유 |
|--------|---------------|
| Django | 풀스택 ORM/Admin 묶음이 무겁다. 마이그레이션 자동화가 raw SQL 의도와 충돌 |
| Tortoise ORM / Piccolo | async-first 매력적이나 SQLAlchemy ecosystem(특히 Alembic, 도구) 우위 |
| MongoDB / NoSQL | 정산 무결성 / FK / TX 가 핵심 — RDBMS 필수 |
| FastAPI built-in OAuth2 password flow | JWT stateless 가 더 단순. 자체 `core/security.py` 가짐 |
| bcrypt | argon2 가 OWASP 권장 (2024+ 기준). bcrypt 는 한도 72byte 와 GPU 약함 |
| uuid v4 만 사용 | v7 의 인덱스 효율 + 시간 정렬 가치 큼 (D-283) |
| Redis (캐시) | 정책 resolve 가 분당 단위 호출 → DB 충분. Post-MVP 시 도입 검토 |
| Celery (작업 큐) | 야간 배치는 APScheduler (Post-MVP) → Celery 까진 과함 |

---

## 10. 디렉토리 구조

```
backend/
├── app/
│   ├── api/v1/                # 라우터 12종
│   ├── core/                  # 횡단 (audit, rbac, response, security, ids, notification)
│   ├── domain/                # pydantic 스키마
│   ├── services/              # 비즈니스 로직 (계산 6 + CRUD)
│   ├── io/excel_export/       # Excel 빌더
│   ├── db/
│   │   ├── session.py
│   │   └── migrations/        # Alembic M001/M002...
│   ├── config.py
│   ├── deps.py
│   └── main.py
├── scripts/internal/          # SaaS 미포함 (picksome_init.py 등)
├── alembic.ini
└── pyproject.toml
```

---

## 11. 다음 단계 (Post-MVP)

- Redis 캐시 (정책 resolve, 자주 조회되는 marketplace_fee_policy)
- Celery / APScheduler — 정산 일자 자동 알림, 정책 검토 자동 표시
- pytest 통합 테스트 + testcontainers (실 PG 격리)
- OpenTelemetry — 분산 트레이싱 (Post-MVP, 다중 인스턴스 시)
