-- ============================================================================
-- DB 역할 부트스트랩 — 환경별 역할과 권한 분리
--
-- 실행: superuser 로 1회.
--   psql -U postgres -d postgres -f backend/scripts/bootstrap_roles.sql
--
-- ⚠️ 이 파일은 **마이그레이션이 아니다.** alembic 에 넣지 않는다.
--    역할·권한은 인프라 관심사라 애플리케이션 마이그레이션이 건드리면 안 된다.
--    운영 DB 에서 alembic 이 GRANT/ALTER ROLE 을 실행하는 순간, 배포가
--    권한 경계를 조용히 바꾸게 된다.
--
-- 배경(2026-08-06):
--   통합 테스트가 테스트 DB 를 만들지 못해 `ALTER ROLE picksome CREATEDB` 를
--   실행한 적이 있다. picksome 은 **운영 애플리케이션 역할**이라 최소권한 위반이다.
--   애플리케이션이 임의로 데이터베이스를 만들 수 있으면, SQL injection 이나
--   실수 한 번으로 디스크를 채울 수 있고 격리 경계도 무너진다.
--   그래서 역할을 분리한다.
-- ============================================================================

-- ── 1. 운영 애플리케이션 역할 ────────────────────────────────────────────
-- 하는 일: 애플리케이션 런타임 쿼리(SELECT/INSERT/UPDATE/DELETE) + alembic 마이그레이션.
-- 갖지 않는 것: CREATEDB, CREATEROLE, SUPERUSER.
--
-- alembic 이 DDL 을 실행하므로 **자기 데이터베이스 안에서의 DDL 권한은 필요**하다.
-- 그건 데이터베이스 소유권으로 충분하고, 새 데이터베이스를 만들 권한과는 다른 얘기다.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'picksome') THEN
    CREATE ROLE picksome LOGIN PASSWORD 'CHANGE_ME';
  END IF;
END $$;

-- 최소권한 원칙 적용. 과거에 부여된 CREATEDB 를 회수한다.
ALTER ROLE picksome NOCREATEDB NOCREATEROLE NOSUPERUSER;

COMMENT ON ROLE picksome IS
  '운영 애플리케이션 역할. 런타임 DML + 자기 DB 내 alembic DDL 전용. '
  'CREATEDB/CREATEROLE 없음 — 테스트 DB 생성은 picksome_test 가 담당.';


-- ── 2. 테스트/CI 전용 역할 ──────────────────────────────────────────────
-- 하는 일: 테스트 데이터베이스 생성·삭제, 테스트 실행.
-- 운영 데이터베이스에는 접근하지 않는다(아래 REVOKE 참조).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'picksome_test') THEN
    CREATE ROLE picksome_test LOGIN PASSWORD 'picksome_test' CREATEDB;
  ELSE
    ALTER ROLE picksome_test CREATEDB;
  END IF;
END $$;

COMMENT ON ROLE picksome_test IS
  '테스트/CI 전용 역할. 테스트 DB 생성 권한만 갖는다. 운영 DB 접속 불가.';

-- 운영 DB 접속 차단. PUBLIC 기본 CONNECT 를 회수하고 운영 역할에만 준다.
-- (이 스크립트를 운영 DB 이름이 다른 환경에서 쓸 때는 아래 이름만 바꾼다)
REVOKE CONNECT ON DATABASE picksome_ops FROM PUBLIC;
GRANT  CONNECT ON DATABASE picksome_ops TO picksome;
REVOKE CONNECT ON DATABASE picksome_ops FROM picksome_test;


-- ── 3. 확인 ─────────────────────────────────────────────────────────────
-- 기대값:
--   picksome       rolcreatedb = false
--   picksome_test  rolcreatedb = true
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole
  FROM pg_roles
 WHERE rolname IN ('picksome', 'picksome_test')
 ORDER BY rolname;
