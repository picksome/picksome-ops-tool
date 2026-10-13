"""증빙 파일 저장소 추상화.

왜 인터페이스인가
----------------
지금은 로컬 파일시스템에 쓴다. 운영에서는 S3 호환 스토리지로 간다.
service 와 router 가 **실제 경로를 알면** 그 교체가 코드 전체를 건드리는 일이 된다.
그래서 바깥에 노출하는 건 `storage_key`(불투명 문자열) 하나뿐이고,
그 키를 무엇으로 해석할지는 구현이 정한다.

`storage_key` 는 **API 응답에 싣지 않는다.** 키가 새면 저장 구조가 드러나고,
버킷 정책이 잘못 열려 있을 때 그대로 접근 경로가 된다.

키 구조
------
    {org_id}/{expense_id}/{attachment_id}.{ext}

원본 파일명을 경로에 쓰지 않는다. 사용자가 올린 이름에는 `../`, 널 바이트,
윈도우 예약어, 제어문자가 들어올 수 있고, 한 번이라도 경로로 해석되면
저장소 밖으로 쓰게 된다. 원본 이름은 **DB 컬럼에만** 두고 표시에만 쓴다.
"""

from __future__ import annotations

import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

# 확장자 화이트리스트. 키를 만들 때만 쓴다 —
# 실제 허용 판정은 service 가 MIME 과 함께 한다.
_SAFE_EXT = re.compile(r"^[a-z0-9]{1,8}$")


class StorageError(RuntimeError):
    """스토리지 계층 실패. service 가 이걸 잡아서 DB 를 되돌린다."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    storage_key: str
    size_bytes: int


class ExpenseAttachmentStorage(ABC):
    """증빙 파일 저장소.

    구현은 키를 받고 키를 돌려준다. **경로를 돌려주지 않는다.**
    """

    @abstractmethod
    def save(self, *, key: str, data: bytes) -> StoredObject:
        """키에 데이터를 쓴다. 이미 있으면 덮어쓴다(같은 키 = 같은 첨부 id)."""

    @abstractmethod
    def open(self, *, key: str) -> BinaryIO:
        """읽기용 스트림. 없으면 `StorageError`."""

    @abstractmethod
    def delete(self, *, key: str) -> None:
        """지운다. **이미 없으면 성공으로 본다** — 재시도가 안전해야 한다."""

    @abstractmethod
    def exists(self, *, key: str) -> bool: ...


def build_key(*, org_id: UUID, expense_id: UUID, attachment_id: UUID, ext: str) -> str:
    """저장 키. 사용자 입력이 **한 글자도** 들어가지 않는다."""
    safe_ext = ext.lower().lstrip(".")
    if not _SAFE_EXT.match(safe_ext):
        # 여기까지 오면 service 검증이 뚫린 것이다. 추측해서 저장하지 않는다.
        raise StorageError(f"허용되지 않는 확장자입니다: {ext!r}")
    return f"{org_id}/{expense_id}/{attachment_id}.{safe_ext}"


class LocalFilesystemStorage(ExpenseAttachmentStorage):
    """개발·테스트용 로컬 파일시스템 구현.

    운영에서 쓰지 않는다 — 여러 인스턴스가 뜨면 각자 다른 디스크를 본다.
    S3 구현으로 갈아끼우면 되고, 그때 이 클래스는 그대로 둔다(테스트가 쓴다).
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """키 → 경로. **root 밖으로 나가면 거부한다.**

        키는 우리가 만들지만, 그 가정이 언젠가 깨진다(예: 다른 코드가 DB 의
        `storage_key` 를 그대로 넘김). 경계 검사는 실제로 파일을 만지는
        마지막 지점에 둔다.
        """
        if "\x00" in key:
            raise StorageError("잘못된 저장 키입니다.")
        candidate = (self._root / key).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise StorageError("저장소 밖 경로에 접근할 수 없습니다.")
        return candidate

    def save(self, *, key: str, data: bytes) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 임시 파일에 쓰고 rename 한다 — 중간에 죽어도 반쪽짜리 파일이
        # 정상 키로 남지 않는다.
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise StorageError(f"파일 저장 실패: {exc}") from exc
        return StoredObject(storage_key=key, size_bytes=len(data))

    def open(self, *, key: str) -> BinaryIO:
        path = self._path(key)
        try:
            return path.open("rb")
        except OSError as exc:
            raise StorageError(f"파일을 열 수 없습니다: {exc}") from exc

    def delete(self, *, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"파일 삭제 실패: {exc}") from exc

    def exists(self, *, key: str) -> bool:
        return self._path(key).is_file()

    # 테스트 정리용. 운영 구현에는 없어도 된다.
    def _wipe(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)
        self._root.mkdir(parents=True, exist_ok=True)


_storage: ExpenseAttachmentStorage | None = None


def get_storage() -> ExpenseAttachmentStorage:
    """프로세스 단일 인스턴스.

    `ATTACHMENT_STORAGE_DIR` 로 루트를 정한다. S3 를 붙일 때는
    `ATTACHMENT_STORAGE_BACKEND` 를 보고 여기서 분기한다 —
    호출부는 바뀌지 않는다.
    """
    global _storage
    if _storage is None:
        from app.config import get_settings

        settings = get_settings()
        _storage = LocalFilesystemStorage(settings.ATTACHMENT_STORAGE_DIR)
    return _storage


def set_storage(storage: ExpenseAttachmentStorage | None) -> None:
    """테스트가 임시 디렉터리·실패 주입 구현으로 갈아끼울 때 쓴다."""
    global _storage
    _storage = storage
