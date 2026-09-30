"""주소 → 처리 함수 표. **주소마다 권한을 적어야 등록된다.**

예전에는 `do_GET`·`do_POST` 가 `if path == "..."` 를 백 개 넘게 늘어놓고, 각
갈래 안에서 `if not can(me, "…")` 를 손으로 적었다. 그러면 두 가지 구멍이 난다.

1. 검사를 **빠뜨려도** 아무도 모른다.
2. 권한 이름을 **잘못 적어도** 아무도 모른다 — `can()` 은 모르는 이름을 관리자
   전용으로 보기 때문에, 실제로 `대시보드_편집` 이 정의되지 않아 채용담당자의
   수식 미리보기가 조용히 막혀 있었다.

여기서는 주소를 등록할 때 권한을 **반드시** 적게 하고(`권한=` 을 안 주면
등록이 안 된다), 서버가 뜰 때 모르는 권한 이름이 있으면 **뜨지 않는다**
(`점검`). 로그인·권한 확인은 처리 함수가 불리기 **전에** 한 군데서 한다.

    @라우트("GET", "/dash/view", 권한="대시보드_조회")
    def get_dash_view(self, me, path):
        ...

권한 자리에 올 수 있는 것:

    "대시보드_조회" 같은 이름  그 권한이 있어야 한다 (`auth._PERMISSIONS`)
    로그인만                  로그인만 하면 된다. 더 좁은 검사는 함수 안에서 한다
                              (현업은 자기 과제 지원자만 — 같은 것)
    공개                      로그인 전에도 된다 (로그인 화면)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

#: 로그인만 하면 되는 주소
로그인만 = "__로그인만__"
#: 로그인 전에도 되는 주소
공개 = "__공개__"


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    func: Callable
    #: 권한 이름 하나, 또는 **그중 하나라도** 있으면 되는 묶음 (`("A", "B")`)
    권한: str | tuple[str, ...]
    #: 권한이 없을 때 보여 줄 말 (비우면 기본 문구)
    거부말: str = ""
    #: 권한이 없을 때 JSON 으로 답한다 (fetch 로 부르는 주소)
    json: bool = False
    #: POST 인데 아무것도 바꾸지 않는다 (미리보기·다시 계산). 캐시를 안 버린다.
    읽기전용: bool = False

    @property
    def 로그인필요(self) -> bool:
        return self.권한 != 공개


class Router:
    def __init__(self) -> None:
        self._표: dict[tuple[str, str], Route] = {}

    def route(self, method: str, *paths: str, 권한: str | tuple[str, ...], 거부말: str = "",
              json: bool = False, 읽기전용: bool = False) -> Callable:
        """처리 함수에 붙이는 표시. 주소를 여러 개 줄 수 있다."""
        if method not in ("GET", "POST"):
            raise ValueError(f"GET/POST 만 됩니다: {method}")
        if not paths:
            raise ValueError("주소가 없습니다")

        def 붙이기(func: Callable) -> Callable:
            for path in paths:
                열쇠 = (method, path)
                옛 = self._표.get(열쇠)
                # 모듈을 다시 읽으면(reload) 같은 함수가 다시 등록된다 — 그건 바꿔 끼운다.
                # **다른** 함수가 같은 주소를 잡으면 한쪽이 조용히 묻히므로 터뜨린다.
                if 옛 is not None and (옛.func.__module__, 옛.func.__qualname__) != (
                        func.__module__, func.__qualname__):
                    raise ValueError(f"같은 주소가 두 번 등록됐습니다: {method} {path} "
                                     f"({옛.func.__qualname__} / {func.__qualname__})")
                self._표[열쇠] = Route(method, path, func, 권한, 거부말, json, 읽기전용)
            return func
        return 붙이기

    def find(self, method: str, path: str) -> Route | None:
        return self._표.get((method, path))

    def all(self) -> list[Route]:
        return sorted(self._표.values(), key=lambda r: (r.method, r.path))

    def 점검(self, 아는권한) -> None:
        """모르는 권한 이름이 있으면 **서버가 뜨지 않게** 터뜨린다."""
        모르는 = sorted({f"{r.method} {r.path} → {이름}" for r in self._표.values()
                       for 이름 in 권한들(r.권한)
                       if 이름 not in (로그인만, 공개) and 이름 not in 아는권한})
        if 모르는:
            raise ValueError("정의되지 않은 권한을 쓰는 주소가 있습니다 "
                             "(cvtool/auth.py 의 _PERMISSIONS 에 추가하세요):\n  "
                             + "\n  ".join(모르는))


def 권한들(권한) -> tuple[str, ...]:
    return tuple(권한) if isinstance(권한, (tuple, list)) else (권한,)


def 통과(me, 권한, can) -> bool:
    """권한 검사. 묶음이면 그중 하나라도 있으면 된다."""
    if 권한 in (로그인만, 공개):
        return True
    return any(can(me, 이름) for 이름 in 권한들(권한))


#: 앱 전체가 쓰는 표 하나
라우터 = Router()
라우트 = 라우터.route
