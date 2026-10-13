"""실제로 구현된 커넥터를 레지스트리에 올린다.

왜 import 부수효과로 하지 않나
--------------------------
모듈을 읽는 것만으로 레지스트리가 바뀌면, 무엇이 등록돼 있는지가 import 순서에
달린다. 테스트가 가짜 커넥터를 넣었다 빼는 순간 순서 의존이 드러나고, 그건
재현이 어렵다.

**두 진입점이 명시적으로 부른다**
```
create_app()          API 프로세스
run_workers.main()    worker 프로세스
```

worker 는 `create_app()` 을 거치지 않는다. 여기서 빠뜨리면 화면에서는 되는
동기화가 자동 실행에서만 "연동 미구현" 으로 막힌다.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("connectors")


def register_builtin() -> list[str]:
    """구현된 커넥터를 등록한다. **여러 번 불러도 안전하다.**"""
    from app.connectors.next_engine.connector import NextEngineConnector
    from app.services import connector as registry

    registered: list[str] = []
    for factory in (NextEngineConnector,):
        instance = factory()
        registry.register(instance)
        registered.append(instance.system_code)

    _log.info("커넥터 등록: %s", ", ".join(registered) or "(없음)")
    return registered
