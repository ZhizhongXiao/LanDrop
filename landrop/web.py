"""Bottle application for LanDrop's browser interface."""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
from pathlib import Path
import re
import secrets
import threading
from typing import Any, BinaryIO, Iterable, Iterator
from urllib.parse import quote, unquote_to_bytes

from bottle import Bottle, HTTPResponse, redirect, request, response, static_file

from .lifecycle import (
    SessionExpiredError,
    SessionLifecycle,
    TransferCancelledError,
    TransferHandle,
)
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


class UploadInterruptedError(StorageError):
    """The client stopped sending before the declared request body ended."""


@dataclass(frozen=True, slots=True)
class WebConfig:
    shared_directory: Path
    receive_directory: Path
    max_upload_bytes: int
    credentials: CredentialStore
    lifecycle: SessionLifecycle | None = None


def create_application(config: WebConfig) -> tuple[Any, str]:
    app = Bottle()
    lifecycle = config.lifecycle or SessionLifecycle()
    csrf_token = secrets.token_urlsafe(24)
    failed_pairing: dict[str, int] = {}
    pairing_lock = threading.Lock()

    def expired_response() -> HTTPResponse | None:
        if lifecycle.accepts_new_requests():
            return None
        return _html_response(
            _page(
                "会话已到期",
                "<main class=\"narrow\"><h1>本次传输会话已到期</h1>"
                "<p>请在电脑上重新启动 LanDrop 服务。</p></main>",
            ),
            503,
        )

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
        response.set_header("X-Frame-Options", "SAMEORIGIN")
        response.set_header("Referrer-Policy", "no-referrer")
        response.set_header("Cache-Control", "no-store")
        response.set_header(
            "Accept-CH",
            "Sec-CH-UA, Sec-CH-UA-Mobile, Sec-CH-UA-Platform, Sec-CH-UA-Model",
        )
        response.set_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; "
            "form-action 'self'; base-uri 'none'",
        )

    @app.get("/")
    def index() -> str:
        expired = expired_response()
        if expired is not None:
            return expired
        client = current_client()
        if client is None:
            return _pairing_page()

        rows = []
        for item in list_shared_files(config.shared_directory):
            url = "/prepare-download/" + quote(item.relative_path, safe="/")
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
            <form id="uploadForm" action="/upload" method="post" enctype="multipart/form-data">
              <input type="hidden" name="csrf" value="{csrf_token}">
              <input id="uploadFile" type="file" name="file" required>
              <button id="uploadButton" type="submit">开始上传</button>
            </form>
            <div id="uploadProgressBox" class="upload-progress" hidden>
              <progress id="uploadProgress" value="0" max="1"></progress>
              <p id="uploadStatus">准备上传……</p>
            </div>
          </section>
          <section class="quiet">
            <form action="/unpair" method="post">
              <input type="hidden" name="csrf" value="{csrf_token}">
              <button type="submit" class="secondary">取消信任此浏览器</button>
            </form>
          </section>
        </main>
        <script>
          const uploadForm = document.getElementById('uploadForm');
          const uploadFile = document.getElementById('uploadFile');
          const uploadButton = document.getElementById('uploadButton');
          const uploadProgressBox = document.getElementById('uploadProgressBox');
          const uploadProgress = document.getElementById('uploadProgress');
          const uploadStatus = document.getElementById('uploadStatus');
          const uploadCsrf = {json.dumps(csrf_token)};

          function uploadMegabytes(bytes) {{
            return (bytes / 1000000).toFixed(2);
          }}

          uploadForm.addEventListener('submit', event => {{
            event.preventDefault();
            const file = uploadFile.files[0];
            if (!file) return;

            uploadButton.disabled = true;
            uploadFile.disabled = true;
            uploadProgressBox.hidden = false;
            uploadProgress.max = Math.max(file.size, 1);
            uploadProgress.value = 0;
            uploadStatus.textContent = '正在建立上传连接……';
            const startedAt = performance.now();
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/upload/raw');
            xhr.setRequestHeader('Content-Type', 'application/octet-stream');
            xhr.setRequestHeader('X-LanDrop-CSRF', uploadCsrf);
            xhr.setRequestHeader('X-LanDrop-Filename', encodeURIComponent(file.name));

            xhr.upload.addEventListener('progress', progressEvent => {{
              if (!progressEvent.lengthComputable) {{
                uploadStatus.textContent = '正在上传，请保持页面打开……';
                return;
              }}
              uploadProgress.max = Math.max(progressEvent.total, 1);
              uploadProgress.value = progressEvent.loaded;
              const elapsedSeconds = Math.max((performance.now() - startedAt) / 1000, 0.001);
              const speed = progressEvent.loaded / elapsedSeconds / 1000000;
              const percent = progressEvent.total
                ? Math.min(100, progressEvent.loaded / progressEvent.total * 100)
                : 0;
              uploadStatus.textContent = `${{percent.toFixed(1)}}% · ${{uploadMegabytes(progressEvent.loaded)}} / ${{uploadMegabytes(progressEvent.total)}} MB · ${{speed.toFixed(2)}} MB/s`;
            }});

            xhr.addEventListener('load', () => {{
              if (xhr.status >= 200 && xhr.status < 300) {{
                document.open();
                document.write(xhr.responseText);
                document.close();
                return;
              }}
              uploadButton.disabled = false;
              uploadFile.disabled = false;
              uploadStatus.textContent = `上传失败（HTTP ${{xhr.status}}），请返回首页重试。`;
            }});
            xhr.addEventListener('error', () => {{
              uploadButton.disabled = false;
              uploadFile.disabled = false;
              uploadStatus.textContent = '上传连接中断；服务可能已停止或网络已经变化。';
            }});
            xhr.addEventListener('abort', () => {{
              uploadButton.disabled = false;
              uploadFile.disabled = false;
              uploadStatus.textContent = '上传已取消。';
            }});
            xhr.send(file);
          }});
        </script>
        """
        return _page("LanDrop", body)

    @app.get("/session-status")
    def session_status() -> HTTPResponse:
        client = current_client()
        if client is None:
            return HTTPResponse(
                body=json.dumps({"error": "authentication"}),
                status=401,
                content_type="application/json; charset=UTF-8",
            )
        snapshot = lifecycle.snapshot()
        status = 200 if snapshot.phase in {"running", "grace"} else 503
        return HTTPResponse(
            body=json.dumps(
                {
                    "phase": snapshot.phase,
                    "remaining_seconds": snapshot.remaining_seconds,
                    "grace_remaining_seconds": snapshot.grace_remaining_seconds,
                    "stop_reason": snapshot.stop_reason,
                },
                ensure_ascii=False,
            ),
            status=status,
            content_type="application/json; charset=UTF-8",
        )

    @app.get("/prepare-download/<filepath:path>")
    def prepare_download(filepath: str) -> HTTPResponse | str:
        expired = expired_response()
        if expired is not None:
            return expired
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
        ticket_url = "/start-download/" + quote(relative, safe="/")
        body = f"""
        <main class="narrow">
          <h1>文件下载</h1>
          <p><strong>{html.escape(candidate.name)}</strong> · {html.escape(format_size(candidate.stat().st_size))}</p>
          <p id="downloadStatus">正在交给浏览器下载……</p>
          <p><button id="downloadLink" type="button">开始或再次下载</button></p>
          <p><a href="/">返回文件列表</a></p>
          <iframe name="downloadTarget" title="下载目标" hidden></iframe>
        </main>
        <script>
          const statusNode = document.getElementById('downloadStatus');
          const link = document.getElementById('downloadLink');
          const ticketUrl = '{html.escape(ticket_url, quote=True)}';
          async function beginDownload() {{
            link.disabled = true;
            try {{
              const response = await fetch(ticketUrl, {{ cache: 'no-store' }});
              if (!response.ok) throw new Error('ticket-failed');
              const ticket = await response.json();
              const anchor = document.createElement('a');
              anchor.href = ticket.download_url;
              anchor.target = 'downloadTarget';
              anchor.download = '';
              anchor.hidden = true;
              document.body.appendChild(anchor);
              anchor.click();
              anchor.remove();
              statusNode.textContent = '已交给浏览器下载；再次点击会创建一个新的文件任务。';
            }} catch (_error) {{
              statusNode.textContent = '无法开始下载，服务可能已停止。';
            }} finally {{
              link.disabled = false;
            }}
          }}
          link.addEventListener('click', beginDownload);
          setTimeout(beginDownload, 100);
          let polling = true;
          async function pollStatus() {{
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), 1500);
            try {{
              const response = await fetch('/session-status', {{
                cache: 'no-store', signal: controller.signal
              }});
              if (!response.ok) throw new Error('service-stopped');
              const state = await response.json();
              if (state.phase === 'grace') {{
                statusNode.textContent = `会话已到期，当前下载处于宽限期，剩余 ${{state.grace_remaining_seconds}} 秒。`;
              }} else {{
                statusNode.textContent = `下载进行中；会话剩余 ${{state.remaining_seconds}} 秒。`;
              }}
            }} catch (_error) {{
              polling = false;
              statusNode.textContent = '服务已停止或网络中断。下载可能显示为“已暂停”；重新启动 LanDrop 后可在浏览器下载管理器中尝试继续。';
            }} finally {{
              clearTimeout(timeout);
              if (polling) setTimeout(pollStatus, 750);
            }}
          }}
          pollStatus();
        </script>
        """
        return _page("下载状态", body)

    @app.get("/start-download/<filepath:path>")
    def start_download(filepath: str) -> HTTPResponse:
        expired = expired_response()
        if expired is not None:
            return expired
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
        download_id = secrets.token_urlsafe(12)
        target = (
            "/download/"
            + quote(relative, safe="/")
            + "?download_id="
            + quote(download_id, safe="")
        )
        return HTTPResponse(
            body=json.dumps({"download_url": target}, ensure_ascii=False),
            status=200,
            content_type="application/json; charset=UTF-8",
        )

    @app.post("/pair")
    def pair() -> HTTPResponse:
        expired = expired_response()
        if expired is not None:
            return expired
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

        submitted = _pairing_digits(request.forms.getunicode("code") or "")
        if not lifecycle.consume_pairing_code(submitted):
            with pairing_lock:
                failed_pairing[remote] = attempts + 1
            return _html_response(
                _page(
                    "配对失败",
                    "<h1>配对失败</h1><p>配对码不正确或已被其他设备使用。"
                    "请查看电脑上当前显示的配对码。</p><p><a href=\"/\">返回</a></p>",
                ),
                403,
            )

        user_agent = request.get_header("User-Agent") or "浏览器"
        device_name = request.forms.getunicode("device_name") or ""
        client_hints = {
            "brands": request.get_header("Sec-CH-UA") or "",
            "mobile": request.get_header("Sec-CH-UA-Mobile") or "",
            "platform": request.get_header("Sec-CH-UA-Platform") or "",
            "model": request.get_header("Sec-CH-UA-Model") or "",
        }
        _client, credential = config.credentials.issue(
            user_agent,
            device_name,
            client_hints,
        )
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
        expired = expired_response()
        if expired is not None:
            return expired
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
        try:
            result = static_file(
                relative,
                root=str(config.shared_directory),
                download=candidate.name,
            )
            submitted_id = request.query.getunicode("download_id") or ""
            download_id = (
                submitted_id
                if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", submitted_id)
                else ""
            )
            content_range = result.get_header("Content-Range") or ""
            range_match = re.match(r"bytes\s+(\d+)-\d+/\d+", content_range)
            response_length = int(result.get_header("Content-Length") or 0)
            transfer = lifecycle.begin_transfer(
                "download",
                download_id=download_id,
                expected_size=candidate.stat().st_size if download_id else response_length,
                range_start=int(range_match.group(1)) if range_match and download_id else 0,
            )
            request.environ["landrop.transfer"] = transfer
            return result
        except SessionExpiredError:
            return expired_response() or _html_response(_error_page(503, "会话已到期。"), 503)

    @app.post("/upload")
    def upload() -> HTTPResponse:
        expired = expired_response()
        if expired is not None:
            return expired
        client = require_client()
        if isinstance(client, HTTPResponse):
            lifecycle.record_rejection("authentication")
            return client

        content_length = request.content_length
        if content_length < 0:
            lifecycle.record_rejection("missing_content_length")
            return _html_response(_error_page(411, "上传请求必须提供 Content-Length。"), 411)
        if content_length > config.max_upload_bytes + MULTIPART_OVERHEAD_ALLOWANCE:
            lifecycle.record_rejection("size_limit")
            return _html_response(
                _error_page(413, f"上传请求超过 {format_size(config.max_upload_bytes)} 上限。"),
                413,
            )
        try:
            ensure_free_space(config.receive_directory, content_length)
        except InsufficientSpaceError as exc:
            lifecycle.record_rejection("insufficient_space")
            return _html_response(_error_page(400, str(exc)), 400)
        if not _valid_csrf(csrf_token):
            lifecycle.record_rejection("csrf")
            return _html_response(_error_page(403, "请求校验失败，请返回首页重试。"), 403)
        try:
            transfer = lifecycle.begin_transfer("upload")
        except SessionExpiredError:
            return expired_response() or _html_response(_error_page(503, "会话已到期。"), 503)
        try:
            uploaded = request.files.get("file")
            if uploaded is None:
                transfer.fail("missing_file")
                return _html_response(_error_page(400, "没有选择上传文件。"), 400)
            result = save_upload(
                uploaded.file,
                uploaded.raw_filename,
                config.receive_directory,
                config.max_upload_bytes,
                progress=transfer.add_bytes,
                check_cancelled=transfer.check_cancelled,
            )
        except UploadTooLargeError as exc:
            transfer.fail("size_limit")
            return _html_response(_error_page(413, str(exc)), 413)
        except (InvalidFilenameError, InsufficientSpaceError, StorageError) as exc:
            transfer.fail("storage_error")
            return _html_response(_error_page(400, str(exc)), 400)
        except TransferCancelledError as exc:
            transfer.fail(exc.reason)
            return _html_response(_error_page(503, "传输会话已经结束。"), 503)
        except Exception:
            transfer.fail("server_error")
            raise
        transfer.complete()

        rename_note = "（因同名已自动重命名）" if result.renamed else ""
        message = (
            f"<h1>上传成功</h1><p>已保存：<strong>{html.escape(result.filename)}</strong>"
            f" {html.escape(format_size(result.size))}{rename_note}</p>"
            '<p><a href="/">返回文件页面</a></p>'
        )
        return _html_response(_page("上传成功", message), 201)

    @app.post("/upload/raw")
    def upload_raw() -> HTTPResponse:
        """Receive one browser file without Bottle buffering multipart data first."""
        expired = expired_response()
        if expired is not None:
            return expired
        client = require_client()
        if isinstance(client, HTTPResponse):
            lifecycle.record_rejection("authentication")
            return client

        submitted_csrf = request.get_header("X-LanDrop-CSRF") or ""
        if not secrets.compare_digest(submitted_csrf, csrf_token):
            lifecycle.record_rejection("csrf")
            return _html_response(_error_page(403, "请求校验失败，请返回首页重试。"), 403)

        try:
            raw_filename = _decode_upload_filename(
                request.get_header("X-LanDrop-Filename") or ""
            )
        except InvalidFilenameError as exc:
            lifecycle.record_rejection("invalid_filename")
            return _html_response(_error_page(400, str(exc)), 400)

        content_length = request.content_length
        if content_length < 0:
            lifecycle.record_rejection("missing_content_length")
            return _html_response(_error_page(411, "上传请求必须提供 Content-Length。"), 411)
        if content_length > config.max_upload_bytes:
            lifecycle.record_rejection("size_limit")
            return _html_response(
                _error_page(413, f"上传请求超过 {format_size(config.max_upload_bytes)} 上限。"),
                413,
            )
        try:
            ensure_free_space(config.receive_directory, content_length)
        except InsufficientSpaceError as exc:
            lifecycle.record_rejection("insufficient_space")
            return _html_response(_error_page(400, str(exc)), 400)

        try:
            transfer = lifecycle.begin_transfer("upload")
        except SessionExpiredError:
            return expired_response() or _html_response(_error_page(503, "会话已到期。"), 503)
        try:
            source = _ContentLengthReader(request.environ["wsgi.input"], content_length)
            result = save_upload(
                source,
                raw_filename,
                config.receive_directory,
                config.max_upload_bytes,
                progress=transfer.add_bytes,
                check_cancelled=transfer.check_cancelled,
            )
        except UploadTooLargeError as exc:
            transfer.fail("size_limit")
            return _html_response(_error_page(413, str(exc)), 413)
        except UploadInterruptedError as exc:
            transfer.fail("client_disconnect")
            return _html_response(_error_page(400, str(exc)), 400)
        except (InvalidFilenameError, InsufficientSpaceError, StorageError) as exc:
            transfer.fail("storage_error")
            return _html_response(_error_page(400, str(exc)), 400)
        except TransferCancelledError as exc:
            transfer.fail(exc.reason)
            return _html_response(_error_page(503, "传输会话已经结束。"), 503)
        except Exception:
            transfer.fail("server_error")
            raise
        transfer.complete()

        rename_note = "（因同名已自动重命名）" if result.renamed else ""
        message = (
            f"<h1>上传成功</h1><p>已保存：<strong>{html.escape(result.filename)}</strong>"
            f" {html.escape(format_size(result.size))}{rename_note}</p>"
            '<p><a href="/">返回文件页面</a></p>'
        )
        return _html_response(_page("上传成功", message), 201)

    @app.post("/unpair")
    def unpair() -> HTTPResponse:
        expired = expired_response()
        if expired is not None:
            return expired
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

    tracked_app = _TrackedApplication(app, lifecycle)
    return tracked_app, lifecycle.pairing_code


def _valid_csrf(expected: str) -> bool:
    submitted = request.forms.getunicode("csrf") or ""
    return secrets.compare_digest(submitted, expected)


def _decode_upload_filename(value: str) -> str:
    if not value or len(value) > 4096:
        raise InvalidFilenameError("文件名为空或过长。")
    try:
        encoded = value.encode("ascii")
        return unquote_to_bytes(encoded.decode("ascii")).decode("utf-8", "strict")
    except (UnicodeEncodeError, UnicodeDecodeError) as exc:
        raise InvalidFilenameError("文件名编码无效。") from exc


class _ContentLengthReader:
    """Expose exactly one HTTP request body and detect an early disconnect."""

    def __init__(self, source: BinaryIO, content_length: int) -> None:
        self._source = source
        self._remaining = content_length

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        requested = self._remaining if size < 0 else min(size, self._remaining)
        try:
            chunk = self._source.read(requested)
        except OSError as exc:
            raise UploadInterruptedError(
                "上传连接提前中断，文件未完整接收。"
            ) from exc
        if not chunk:
            raise UploadInterruptedError("上传连接提前中断，文件未完整接收。")
        if len(chunk) > self._remaining:
            chunk = chunk[: self._remaining]
        self._remaining -= len(chunk)
        return chunk


def _pairing_page() -> str:
    body = """
    <main class="narrow">
      <h1>连接 LanDrop</h1>
      <p>请在电脑的 LanDrop 窗口或控制台中查看当前 8 位配对码。配对成功后该码会立即更新。</p>
      <form action="/pair" method="post">
        <label>配对码 <input id="pairingCode" name="code" inputmode="numeric"
          autocomplete="one-time-code" pattern="[0-9]{8}" required></label>
        <label>设备名称（可选）
          <input name="device_name" maxlength="40" autocomplete="off"
            placeholder="例如：XXX 的 iPhone 16">
        </label>
        <button type="submit">配对</button>
      </form>
    </main>
    <script>
      const pairingCode = document.getElementById('pairingCode');
      pairingCode.addEventListener('input', () => {
        pairingCode.value = pairingCode.value.replace(/[^0-9]/g, '').slice(0, 8);
      });
      pairingCode.addEventListener('paste', event => {
        event.preventDefault();
        const pasted = (event.clipboardData || window.clipboardData).getData('text');
        pairingCode.value = pasted.replace(/[^0-9]/g, '').slice(0, 8);
        pairingCode.dispatchEvent(new Event('input', { bubbles: true }));
      });
    </script>
    """
    return _page("连接 LanDrop", body)


def _pairing_digits(value: str) -> str:
    return "".join(character for character in value if "0" <= character <= "9")


def _error_page(code: int, message: str) -> str:
    return _page(
        f"错误 {code}",
        f"<main class=\"narrow\"><h1>错误 {code}</h1><p>{html.escape(message)}</p>"
        '<p><a href="/">返回首页</a></p></main>',
    )


def _html_response(body: str, status: int) -> HTTPResponse:
    return HTTPResponse(body=body, status=status, content_type="text/html; charset=UTF-8")


class _TrackedApplication:
    """Wrap WSGI response iteration so downloads count actual sent bytes."""

    def __init__(self, application: Bottle, lifecycle: SessionLifecycle) -> None:
        self._application = application
        self._lifecycle = lifecycle

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
        iterable = self._application(environ, start_response)
        transfer = environ.pop("landrop.transfer", None)
        if not isinstance(transfer, TransferHandle):
            return iterable
        return _TrackedIterable(iterable, transfer)


class _TrackedIterable(Iterator[bytes]):
    def __init__(self, iterable: Iterable[bytes], transfer: TransferHandle) -> None:
        self._iterable = iterable
        self._iterator = iter(iterable)
        self._transfer = transfer
        self._finished = False

    def __iter__(self) -> _TrackedIterable:
        return self

    def __next__(self) -> bytes:
        try:
            self._transfer.check_cancelled()
            chunk = next(self._iterator)
            self._transfer.add_bytes(len(chunk))
            return chunk
        except StopIteration:
            self._finished = True
            self._transfer.complete()
            raise
        except TransferCancelledError as exc:
            self._finished = True
            self._transfer.fail(exc.reason)
            close = getattr(self._iterable, "close", None)
            if close is not None:
                close()
            raise StopIteration
        except Exception:
            self._finished = True
            self._transfer.fail("server_error")
            raise

    def close(self) -> None:
        close = getattr(self._iterable, "close", None)
        if close is not None:
            close()
        if not self._finished:
            self._finished = True
            self._transfer.fail("client_disconnect")


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
    button:disabled, input:disabled {{ opacity: .6; cursor: wait; }}
    .upload-progress {{ width: 100%; margin-top: 12px; }}
    .upload-progress progress {{ width: 100%; height: 16px; }}
    .upload-progress p {{ margin: 6px 0 0; overflow-wrap: anywhere; }}
    .files {{ list-style: none; padding: 0; margin: 0; }}
    .files li {{ display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid #dfe5ed; }}
    .files a {{ overflow-wrap: anywhere; }}
    .quiet {{ box-shadow: none; background: transparent; padding: 0; }}
    @media (prefers-color-scheme: dark) {{ body {{ background: #111722; color: #edf3fa; }} section, .narrow {{ background: #1b2431; }} }}
  </style>
</head>
<body>{body}</body>
</html>"""
