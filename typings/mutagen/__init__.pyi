from os import PathLike
from typing import Any, BinaryIO

class MutagenError(Exception): ...

class StreamInfo:
    length: float

class Tags:
    def get(self, key: str, default: Any = ...) -> list[str] | None: ...

class FileType:
    info: StreamInfo
    tags: Tags | None

def File(
    filething: str | PathLike[str] | BinaryIO,
    options: Any = ...,
    easy: bool = ...,
) -> FileType | None: ...
