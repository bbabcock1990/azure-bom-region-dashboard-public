"""
Minimal stand-in for the slice of ``azure.functions`` this app used.

Replacing the Azure Functions host with a plain web server (see ``server/``)
means handlers no longer receive a real ``azure.functions.HttpRequest`` /
return an ``azure.functions.HttpResponse``. This module provides drop-in
``HttpRequest`` / ``HttpResponse`` types with the exact surface the handlers
call, so handler bodies (``def main(req): ...``) did not have to change:

Request:  method, url, headers (case-insensitive .get), params (.get),
          route_params (.get), get_json() (raises ValueError on bad JSON),
          get_body(), form (.get), files (.get -> file with .read()/.filename)
Response: HttpResponse(body, status_code=, mimetype=, headers=)

The ``server`` layer is responsible for populating an ``HttpRequest`` from the
incoming web request and turning the returned ``HttpResponse`` into a real
HTTP response.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union


class _CaseInsensitiveHeaders:
    def __init__(self, items: Optional[Dict[str, str]] = None):
        self._d: Dict[str, str] = {}
        if items:
            for k, v in items.items():
                self._d[k.lower()] = v

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get((key or "").lower(), default)

    def __getitem__(self, key: str) -> str:
        return self._d[(key or "").lower()]

    def __contains__(self, key: str) -> bool:
        return (key or "").lower() in self._d


class _Mapping:
    """Read-only dict-like wrapper exposing ``.get`` (mirrors the subset of
    Werkzeug MultiDict the handlers use)."""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self._d = dict(data or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def __iter__(self):
        return iter(self._d)


class UploadedFile:
    """A single uploaded file. ``.read()`` returns the full bytes;
    ``.filename`` is the client-provided name (may be None)."""

    def __init__(self, filename: Optional[str], content: bytes):
        self.filename = filename
        self._content = content or b""

    def read(self, *args, **kwargs) -> bytes:
        return self._content


class HttpResponse:
    def __init__(
        self,
        body: Union[str, bytes, None] = None,
        *,
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        mimetype: str = "text/plain",
        charset: str = "utf-8",
    ):
        self.status_code = status_code
        self.mimetype = mimetype
        self.charset = charset
        self.headers = dict(headers or {})
        if body is None:
            self._body = b""
        elif isinstance(body, str):
            self._body = body.encode(charset)
        else:
            self._body = bytes(body)

    def get_body(self) -> bytes:
        return self._body


class HttpRequest:
    def __init__(
        self,
        *,
        method: str,
        url: str = "",
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
        route_params: Optional[Dict[str, str]] = None,
        body: bytes = b"",
        form: Optional[Dict[str, str]] = None,
        files: Optional[Dict[str, UploadedFile]] = None,
    ):
        self.method = (method or "GET").upper()
        self.url = url
        self.headers = _CaseInsensitiveHeaders(headers)
        self.params = _Mapping(params)
        self.route_params = _Mapping(route_params)
        self._body = body or b""
        self._form = _Mapping(form)
        self._files = _Mapping(files)

    @property
    def form(self) -> _Mapping:
        return self._form

    @property
    def files(self) -> _Mapping:
        return self._files

    def get_body(self) -> bytes:
        return self._body

    def get_json(self) -> Any:
        if not self._body:
            raise ValueError("request body is empty")
        try:
            return json.loads(self._body)
        except json.JSONDecodeError as ex:
            raise ValueError(str(ex)) from ex
