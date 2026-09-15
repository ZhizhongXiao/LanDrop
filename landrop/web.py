"""Bottle application for LanDrop's browser interface."""

from __future__ import annotations

from dataclasses import dataclass
import html
from pathlib import Path
import secrets
import threading
from urllib.parse import quote

from bottle import Bottle, HTTPResponse, redirect, request, response, static_file

from .storage import (
    InsufficientSpaceError,
    InvalidFilenameError,
    StorageError,
    UploadTooLargeError,
    ensure_free_space,
    format_size,
    list_shared_files,
    resolve_shared_file,
    save_upload,
)
from .trust import CredentialStore, TrustedClient


COOKIE_NAME = "landrop_trust"
PAIRING_ATTEMPT_LIMIT = 5
MULTIPART_OVERHEAD_ALLOWANCE = 2 * 1024 * 1024
SMALL_FORM_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class WebConfig:
    shared_directory: Path
    receive_directory: Path
    max_upload_bytes: int
    credentials: CredentialStore


def create_application(config: WebConfig) -> tuple[Bottle, str]:
    app = Bottle()
    pairing_code = f"{secrets.randbelow(100_000_000):08d}"
    csrf_token = secrets.token_urlsafe(24)
    failed_pairing: dict[str, int] = {}
    pairing_lock = threading.Lock()

    def current_client() -> TrustedClient | None:
        return config.credentials.verify(request.get_cookie(COOKIE_NAME))

    def require_client() -> TrustedClient | HTTPResponse:
        client = current_client()
        if client is None:
            return _html_response(
                _page("需要配对", "<h1>需要配对</h1><p>请先返回首页完成配对。</p>"),
                401,
            )
        return client

    @app.hook("after_request")
    def security_headers() -> None:
        response.set_header("X-Content-Type-Options", "nosniff")
        response.set_header("X-Frame-Options", "DENY")
        response.set_header("Referrer-Policy", "no-referrer")
        response.set_header("Cache-Control", "no-store")
        response.set_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'",
        )

    @app.get("/")
    def index() -> str:
        client = current_client()
        if client is None:
            return _pairing_page()

        rows = []
        for item in list_shared_files(config.shared_directory):
            url = "/download/" + quote(item.relative_path, safe="/")
            rows.append(
                "<li><a href=\"{}\">{}</a><span>{}</span></li>".format(
                    html.escape(url, quote=True),
                    html.escape(item.relative_path),
                    html.escape(format_size(item.size)),
                )
            )
        listing = "".join(rows) or "<li><em>共享目录中暂无文件</em></li>"
        body = f"""
        <header><div><h1>LanDrop</h1><p>可信客户端：{html.escape(client.label)}</p></div></header>
        <main>
          <section>
            <h2>从电脑下载</h2>
            <ul class="files">{listing}</ul>
          </section>
          <section>
            <h2>上传到电脑</h2>
            <p>单个文件上限：{html.escape(format_size(config.max_upload_bytes))}</p>
            <form action="/upload" method="post" enctype="multipart/form-data">
              <input type="hidden" name="csrf" value="{csrf_token}">
              <input type="file" name="file" required>
              <button type="submit">开始上传</button>
            </form>
          </section>
          <section class="quiet">
            <form action="/unpair" method="post">
              <input type="hidden" name="csrf" value="{csrf_token}">
              <button type="submit" class="secondary">取消信任此浏览器</button>
            </form>
          </section>
        </main>
        """
        return _page("LanDrop", body)

    @app.post("/pair")
    def pair() -> HTTPResponse:
        remote = request.remote_addr or "unknown"
        with pairing_lock:
            attempts = failed_pairing.get(remote, 0)
        if attempts >= PAIRING_ATTEMPT_LIMIT:
            return _html_response(
                _page("配对受限", "<h1>尝试次数过多</h1><p>请重启服务以重新生成配对码。</p>"),
                429,
            )

        if request.content_length < 0 or request.content_length > SMALL_FORM_LIMIT:
            return _html_response(_error_page(413, "配对请求大小无效。"), 413)

        submitted = (request.forms.getunicode("code") or "").strip()
        if not secrets.compare_digest(submitted, pairing_code):
            with pairing_lock:
                failed_pairing[remote] = attempts + 1
            return _html_response(
                _page("配对失败", "<h1>配对失败</h1><p>配对码不正确。</p><p><a href=\"/\">返回</a></p>"),
                403,
            )

        label = request.get_header("User-Agent") or "浏览器"
        _client, credential = config.credentials.issue(label)
        response.set_cookie(
            COOKIE_NAME,
            credential,
            path="/",
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="Strict",
        )
        with pairing_lock:
            failed_pairing.pop(remote, None)
        return redirect("/", code=303)

    @app.get("/download/<filepath:path>")
    def download(filepath: str) -> HTTPResponse:
        client = require_client()
        if isinstance(client, HTTPResponse):
            return client
        try:
            candidate = resolve_shared_file(config.shared_directory, filepath)
        except InvalidFilenameError as exc:
            return _html_response(_error_page(403, str(exc)), 403)
        except FileNotFoundError:
            return _html_response(_error_page(404, "请求文件不存在。"), 404)
        relative = candidate.relative_to(config.shared_directory.resolve(strict=True)).as_posix()
        return static_file(
            relative,
            root=str(config.shared_directory),
            download=candidate.name,
        )

    @app.post("/upload")
    def upload() -> HTTPResponse:
        client = require_client()
        if isinstance(client, HTTPResponse):
            return client

        content_length = request.content_length
        if content_length < 0:
            return _html_response(_error_page(411, "上传请求必须提供 Content-Length。"), 411)
        if content_length > config.max_upload_bytes + MULTIPART_OVERHEAD_ALLOWANCE:
            return _html_response(
                _error_page(413, f"上传请求超过 {format_size(config.max_upload_bytes)} 上限。"),
                413,
            )
        try:
            ensure_free_space(config.receive_directory, content_length)
        except InsufficientSpaceError as exc:
            return _html_response(_error_page(400, str(exc)), 400)
        if not _valid_csrf(csrf_token):
            return _html_response(_error_page(403, "请求校验失败，请返回首页重试。"), 403)
        try:
            uploaded = request.files.get("file")
            if uploaded is None:
                return _html_response(_error_page(400, "没有选择上传文件。"), 400)
            result = save_upload(
                uploaded.file,
                uploaded.raw_filename,
                config.receive_directory,
                config.max_upload_bytes,
            )
        except UploadTooLargeError as exc:
            return _html_response(_error_page(413, str(exc)), 413)
        except (InvalidFilenameError, InsufficientSpaceError, StorageError) as exc:
            return _html_response(_error_page(400, str(exc)), 400)

        rename_note = "（因同名已自动重命名）" if result.renamed else ""
        message = (
            f"<h1>上传成功</h1><p>已保存：<strong>{html.escape(result.filename)}</strong>"
            f" {html.escape(format_size(result.size))}{rename_note}</p>"
            '<p><a href="/">返回文件页面</a></p>'
        )
        return _html_response(_page("上传成功", message), 201)

    @app.post("/unpair")
    def unpair() -> HTTPResponse:
        client = require_client()
        if isinstance(client, HTTPResponse):
            return client
        if request.content_length < 0 or request.content_length > SMALL_FORM_LIMIT:
            return _html_response(_error_page(413, "请求大小无效。"), 413)
        if not _valid_csrf(csrf_token):
            return _html_response(_error_page(403, "请求校验失败。"), 403)
        config.credentials.revoke(client.client_id)
        response.delete_cookie(COOKIE_NAME, path="/")
        return redirect("/", code=303)

    @app.error(404)
    def not_found(_error: object) -> HTTPResponse:
        return _html_response(_error_page(404, "请求页面或文件不存在。"), 404)

    @app.error(405)
    def method_not_allowed(_error: object) -> HTTPResponse:
        return _html_response(_error_page(405, "此地址不允许使用该请求方法。"), 405)

    return app, pairing_code


def _valid_csrf(expected: str) -> bool:
    submitted = request.forms.getunicode("csrf") or ""
    return secrets.compare_digest(submitted, expected)


def _pairing_page() -> str:
    body = """
    <main class="narrow">
      <h1>连接 LanDrop</h1>
      <p>请在电脑的 LanDrop 终端中查看本次服务的 8 位配对码。</p>
      <form action="/pair" method="post">
        <label>配对码 <input name="code" inputmode="numeric" pattern="[0-9]{8}" maxlength="8" required></label>
        <button type="submit">配对</button>
      </form>
    </main>
    """
    return _page("连接 LanDrop", body)


def _error_page(code: int, message: str) -> str:
    return _page(
        f"错误 {code}",
        f"<main class=\"narrow\"><h1>错误 {code}</h1><p>{html.escape(message)}</p>"
        '<p><a href="/">返回首页</a></p></main>',
    )


def _html_response(body: str, status: int) -> HTTPResponse:
    return HTTPResponse(body=body, status=status, content_type="text/html; charset=UTF-8")


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; background: #f5f7fb; color: #182230; }}
    header, main {{ width: min(760px, calc(100% - 32px)); margin: 24px auto; }}
    main {{ display: grid; gap: 18px; }}
    section, .narrow {{ background: white; border-radius: 14px; padding: 20px; box-shadow: 0 5px 20px #18223012; }}
    h1, h2 {{ margin-top: 0; }}
    form {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }}
    input, button {{ font: inherit; padding: 10px 12px; }}
    button {{ border: 0; border-radius: 8px; background: #1264d8; color: white; cursor: pointer; }}
    button.secondary {{ background: #596579; }}
    .files {{ list-style: none; padding: 0; margin: 0; }}
    .files li {{ display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid #dfe5ed; }}
    .files a {{ overflow-wrap: anywhere; }}
    .quiet {{ box-shadow: none; background: transparent; padding: 0; }}
    @media (prefers-color-scheme: dark) {{ body {{ background: #111722; color: #edf3fa; }} section, .narrow {{ background: #1b2431; }} }}
  </style>
</head>
<body>{body}</body>
</html>"""
