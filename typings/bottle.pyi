from collections.abc import Callable, Iterable, Mapping
from typing import BinaryIO, TypeVar

_Callback = TypeVar("_Callback", bound=Callable[..., object])

class FormsDict:
    def getunicode(self, name: str, default: str | None = None) -> str | None: ...

class FileUpload:
    filename: str
    raw_filename: str
    file: BinaryIO

class FileUploadDict:
    def get(self, name: str, default: None = None) -> FileUpload | None: ...

class QueryDict:
    def getunicode(self, name: str, default: str | None = None) -> str | None: ...

class BaseRequest:
    remote_addr: str | None
    content_length: int
    forms: FormsDict
    files: FileUploadDict
    query: QueryDict
    environ: dict[str, object]
    def get_cookie(self, key: str, default: str | None = None) -> str | None: ...
    def get_header(self, name: str, default: str | None = None) -> str | None: ...

class BaseResponse:
    def set_cookie(
        self,
        name: str,
        value: str,
        *,
        path: str = ...,
        max_age: int | None = ...,
        httponly: bool = ...,
        samesite: str | None = ...,
    ) -> None: ...
    def set_header(self, name: str, value: str) -> None: ...

class HTTPResponse:
    def __init__(
        self,
        body: object = ...,
        status: int | str | None = ...,
        headers: Mapping[str, str] | None = ...,
        **more_headers: str,
    ) -> None: ...
    def get_header(self, name: str, default: str | None = None) -> str | None: ...

class Bottle:
    def hook(self, name: str) -> Callable[[_Callback], _Callback]: ...
    def get(self, path: str) -> Callable[[_Callback], _Callback]: ...
    def post(self, path: str) -> Callable[[_Callback], _Callback]: ...
    def error(self, code: int) -> Callable[[_Callback], _Callback]: ...
    def __call__(
        self,
        environ: dict[str, object],
        start_response: Callable[[str, list[tuple[str, str]]], object],
    ) -> Iterable[bytes]: ...

request: BaseRequest
response: BaseResponse

def redirect(url: str, code: int | None = None) -> HTTPResponse: ...
def static_file(
    filename: str,
    root: str,
    mimetype: bool | str = True,
    download: bool | str = False,
    charset: str = "UTF-8",
    etag: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> HTTPResponse: ...
