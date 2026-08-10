"""multipart/form-data 파서 (표준 라이브러리만).

cgi 모듈은 3.13 에서 제거됐고 폐쇄망이라 외부 패키지도 못 쓰므로 직접 파싱한다.
파일 업로드(여러 개)와 일반 필드를 함께 다룬다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DISPOSITION_RE = re.compile(rb'name="([^"]*)"')
_FILENAME_RE = re.compile(rb'filename="([^"]*)"')


@dataclass
class UploadedFile:
    filename: str
    content: bytes


@dataclass
class FormData:
    fields: dict[str, str] = field(default_factory=dict)
    files: list[UploadedFile] = field(default_factory=list)


def parse_boundary(content_type: str) -> bytes | None:
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            value = part[len("boundary=") :].strip().strip('"')
            return value.encode()
    return None


def parse_multipart(body: bytes, content_type: str) -> FormData:
    """multipart 본문을 FormData 로 파싱."""
    form = FormData()
    boundary = parse_boundary(content_type)
    if not boundary:
        return form

    delimiter = b"--" + boundary
    for chunk in body.split(delimiter):
        if not chunk or chunk in (b"--\r\n", b"--", b"\r\n"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        head, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        # 파트 끝의 CRLF 제거
        if content.endswith(b"\r\n"):
            content = content[:-2]

        name_m = _DISPOSITION_RE.search(head)
        if not name_m:
            continue
        name = name_m.group(1).decode("utf-8", "replace")

        file_m = _FILENAME_RE.search(head)
        if file_m:
            filename = file_m.group(1).decode("utf-8", "replace")
            if filename:  # 빈 파일 입력칸은 무시
                form.files.append(UploadedFile(filename=filename, content=content))
        else:
            form.fields[name] = content.decode("utf-8", "replace")
    return form
