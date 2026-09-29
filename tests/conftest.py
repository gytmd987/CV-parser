"""여러 테스트 파일이 같이 쓰는 웹 클라이언트.

다른 파일들은 저마다 `web` 을 만들어 쓴다 (그대로 둔다). 새 파일은 이것을 쓴다.
웹 모듈은 import 시점에 DATA_DIR 에 DB 를 만들기 때문에, 파일마다 임시 폴더를
가리키게 한 뒤 **다시 읽는다**(reload).
"""

from __future__ import annotations

import importlib
import os
import threading
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture(scope="module")
def web_client(tmp_path_factory):
    data = tmp_path_factory.mktemp("cvdata")
    os.environ["CVTOOL_DATA_DIR"] = str(data)
    os.environ["CVTOOL_ADMIN_PASSWORD"] = "pw1234"
    os.environ["CVTOOL_ADMIN_ID"] = "admin"
    mod = importlib.reload(importlib.import_module("cvtool.web.app"))
    mod.bootstrap_admin()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class Client:
        module = mod

        def __init__(self):
            self.opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(CookieJar()))

        def post(self, path: str, **fields):
            body = urllib.parse.urlencode(fields, doseq=True, encoding="utf-8").encode()
            try:
                with self.opener.open(urllib.request.Request(base + path, data=body),
                                      timeout=20) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

        def get(self, path: str) -> str:
            with self.opener.open(base + path, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

        def login(self, 아이디: str, 비번: str) -> "Client":
            self.post("/login", userid=아이디, password=비번)
            return self

    Client.new = staticmethod(Client)
    c = Client().login("admin", "pw1234")
    yield c
    server.shutdown()
