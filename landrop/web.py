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
                "<p>请在服务机上重新启动 LanDrop 服务。</p></main>",
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

    def complete_new_client_pairing(
        kind: str,
        submitted: str,
        device_name: str,
        remote: str,
    ) -> tuple[str, str]:
        """Commit both pairing methods through one serialized transaction."""
        with pairing_lock:
            attempts = failed_pairing.get(remote, 0)
            if kind == "code" and attempts >= PAIRING_ATTEMPT_LIMIT:
                return "limited", ""

            pairing = lifecycle.prepare_pairing(kind, submitted)
            if pairing is None:
                if kind == "code":
                    failed_pairing[remote] = attempts + 1
                return "invalid", ""

            user_agent = request.get_header("User-Agent") or "浏览器"
            client_hints = {
                "brands": request.get_header("Sec-CH-UA") or "",
                "mobile": request.get_header("Sec-CH-UA-Mobile") or "",
                "platform": request.get_header("Sec-CH-UA-Platform") or "",
                "model": request.get_header("Sec-CH-UA-Model") or "",
            }
            prepared_credential = config.credentials.prepare(
                user_agent,
                device_name,
                client_hints,
            )
            try:
                with config.credentials.persist_prepared(prepared_credential) as persistence:
                    if not lifecycle.commit_pairing(pairing):
                        return "unavailable", ""
                    persistence.commit()
            except (OSError, RuntimeError):
                return "storage_error", ""

            failed_pairing.pop(remote, None)
            return "success", prepared_credential.credential

    def trusted_cookie_response(credential: str, *, redirect_to: str = "/") -> HTTPResponse:
        response.set_cookie(
            COOKIE_NAME,
            credential,
            path="/",
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="Strict",
        )
        return redirect(redirect_to, code=303)

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
            ticket_url = "/start-download/" + quote(item.relative_path, safe="/")
            rows.append(
                "<li class=\"file-row\"><label class=\"file-choice\">"
                "<input class=\"download-choice\" type=\"checkbox\" data-ticket-url=\"{}\">"
                "<a class=\"file-link\" href=\"{}\" data-ticket-url=\"{}\">{}</a>"
                "</label><span class=\"file-size\">{}</span></li>".format(
                    html.escape(ticket_url, quote=True),
                    html.escape(url, quote=True),
                    html.escape(ticket_url, quote=True),
                    html.escape(item.relative_path),
                    html.escape(format_size(item.size)),
                )
            )
        listing = "".join(rows) or '<li class="empty"><em>当前没有可供下载的文件</em></li>'
        body = f"""
        <header><div><h1>LanDrop</h1><p>可信客户机浏览器：{html.escape(client.label)}</p></div></header>
        <main>
          <section>
            <h2>从服务机下载</h2>
            <ul class="files">{listing}</ul>
            <p id="downloadStatus" class="transfer-status" aria-live="polite">选择文件名可直接下载，也可勾选多个文件。</p>
            <iframe name="downloadTarget" title="下载目标" hidden></iframe>
          </section>
          <section>
            <h2>上传到服务机</h2>
            <p>单个文件上限：{html.escape(format_size(config.max_upload_bytes))}</p>
            <form id="uploadForm" action="/upload" method="post" enctype="multipart/form-data">
              <input type="hidden" name="csrf" value="{csrf_token}">
              <input id="uploadFile" type="file" name="file" multiple required>
              <button id="uploadButton" type="submit">开始串行上传</button>
              <button id="uploadCancel" class="secondary" type="button" hidden>停止整批</button>
              <button id="uploadRetry" class="secondary" type="button" hidden>重试失败项</button>
            </form>
            <div id="uploadProgressBox" class="upload-progress" hidden>
              <progress id="uploadProgress" value="0" max="1"></progress>
              <p id="uploadStatus">准备上传……</p>
              <ul id="uploadResults" class="batch-results"></ul>
            </div>
          </section>
          <section class="quiet">
            <form action="/unpair" method="post">
              <input type="hidden" name="csrf" value="{csrf_token}">
              <button type="submit" class="secondary">取消信任此浏览器</button>
            </form>
          </section>
        </main>
        <div class="bulk-bar" role="group" aria-label="批量下载">
          <span id="downloadSelection">尚未选择文件</span>
          <button id="downloadSelected" type="button" disabled>下载所选文件</button>
        </div>
        <script>
          const downloadStatus = document.getElementById('downloadStatus');
          const downloadChoices = [...document.querySelectorAll('.download-choice')];
          const downloadSelected = document.getElementById('downloadSelected');
          const downloadSelection = document.getElementById('downloadSelection');
          const uploadForm = document.getElementById('uploadForm');
          const uploadFile = document.getElementById('uploadFile');
          const uploadButton = document.getElementById('uploadButton');
          const uploadCancel = document.getElementById('uploadCancel');
          const uploadRetry = document.getElementById('uploadRetry');
          const uploadProgressBox = document.getElementById('uploadProgressBox');
          const uploadProgress = document.getElementById('uploadProgress');
          const uploadStatus = document.getElementById('uploadStatus');
          const uploadResults = document.getElementById('uploadResults');
          const uploadCsrf = {json.dumps(csrf_token)};
          let currentUpload = null;
          let stopUploadQueue = false;
          let failedUploads = [];

          function uploadMegabytes(bytes) {{
            return (bytes / 1000000).toFixed(2);
          }}

          function updateDownloadSelection() {{
            const count = downloadChoices.filter(item => item.checked).length;
            downloadSelection.textContent = count ? `已选择 ${{count}} 个文件` : '尚未选择文件';
            downloadSelected.disabled = count === 0;
          }}

          async function beginDownload(ticketUrl, label, batchId = '') {{
            const ticket = new URL(ticketUrl, window.location.href);
            if (batchId) ticket.searchParams.set('batch_id', batchId);
            const response = await fetch(ticket, {{ cache: 'no-store' }});
            if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
            const ticketData = await response.json();
            const anchor = document.createElement('a');
            anchor.href = ticketData.download_url;
            anchor.target = 'downloadTarget';
            anchor.download = '';
            anchor.hidden = true;
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
            downloadStatus.textContent = `已将“${{label}}”交给浏览器下载。`;
            return ticketData;
          }}

          async function waitForLogicalDownload(ticketData, label) {{
            while (true) {{
              await new Promise(resolve => setTimeout(resolve, 1000));
              const response = await fetch(ticketData.status_url, {{ cache: 'no-store' }});
              if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
              const state = await response.json();
              if (state.status === 'completed') return;
              if (state.status === 'failed' || state.status === 'missing') {{
                throw new Error(state.status);
              }}
              downloadStatus.textContent = state.status === 'waiting_retry'
                ? `“${{label}}”正在等待浏览器继续下载……`
                : `正在下载“${{label}}”；完成后将开始下一项。`;
            }}
          }}

          for (const link of document.querySelectorAll('.file-link')) {{
            link.addEventListener('click', async event => {{
              event.preventDefault();
              try {{
                await beginDownload(link.dataset.ticketUrl, link.textContent);
              }} catch (_error) {{
                downloadStatus.textContent = '无法开始下载；服务可能已停止或网络已经变化。';
              }}
            }});
          }}
          for (const choice of downloadChoices) choice.addEventListener('change', updateDownloadSelection);
          downloadSelected.addEventListener('click', async () => {{
            const selected = downloadChoices.filter(item => item.checked);
            if (!selected.length || !confirm(`确定依次下载所选的 ${{selected.length}} 个文件吗？浏览器可能询问是否允许多文件下载。`)) return;
            downloadSelected.disabled = true;
            const batchId = Array.from(crypto.getRandomValues(new Uint8Array(12)), value => value.toString(16).padStart(2, '0')).join('');
            let started = 0;
            for (const [index, choice] of selected.entries()) {{
              const label = choice.closest('.file-row').querySelector('.file-link').textContent;
              downloadStatus.textContent = `正在开始第 ${{index + 1}} / ${{selected.length}} 个文件：${{label}}`;
              try {{
                const ticketData = await beginDownload(choice.dataset.ticketUrl, label, batchId);
                await waitForLogicalDownload(ticketData, label);
                started += 1;
              }} catch (_error) {{
                downloadStatus.textContent = `第 ${{index + 1}} 个文件未能开始；已停止本批次。`;
                break;
              }}
            }}
            downloadStatus.textContent = `本批次已完成 ${{started}} / ${{selected.length}} 个文件。`;
            downloadSelected.disabled = false;
          }});

          function setUploadControls(running) {{
            uploadButton.disabled = running;
            uploadFile.disabled = running;
            uploadCancel.hidden = !running;
          }}

          function appendUploadResult(file, message, success) {{
            const row = document.createElement('li');
            row.className = success ? 'success' : 'failure';
            row.textContent = `${{file.name}}：${{message}}`;
            uploadResults.appendChild(row);
          }}

          function uploadOne(file, index, total, completedBytes, totalBytes) {{
            return new Promise(resolve => {{
              const startedAt = performance.now();
              const xhr = new XMLHttpRequest();
              currentUpload = xhr;
              xhr.open('POST', '/upload/raw');
              xhr.setRequestHeader('Content-Type', 'application/octet-stream');
              xhr.setRequestHeader('X-LanDrop-CSRF', uploadCsrf);
              xhr.setRequestHeader('X-LanDrop-Filename', encodeURIComponent(file.name));
              xhr.upload.addEventListener('progress', progressEvent => {{
                const loaded = progressEvent.lengthComputable ? progressEvent.loaded : 0;
                uploadProgress.max = Math.max(totalBytes, 1);
                uploadProgress.value = Math.min(totalBytes, completedBytes + loaded);
                const elapsedSeconds = Math.max((performance.now() - startedAt) / 1000, 0.001);
                const speed = loaded / elapsedSeconds / 1000000;
                const percent = file.size ? Math.min(100, loaded / file.size * 100) : 100;
                uploadStatus.textContent = `第 ${{index + 1}} / ${{total}} 个：${{file.name}} · ${{percent.toFixed(1)}}% · ${{uploadMegabytes(loaded)}} / ${{uploadMegabytes(file.size)}} MB · ${{speed.toFixed(2)}} MB/s`;
              }});
              xhr.addEventListener('load', () => resolve({{ ok: xhr.status >= 200 && xhr.status < 300, status: `HTTP ${{xhr.status}}` }}));
              xhr.addEventListener('error', () => resolve({{ ok: false, status: '连接中断' }}));
              xhr.addEventListener('abort', () => resolve({{ ok: false, status: '已取消' }}));
              xhr.send(file);
            }});
          }}

          async function runUploadQueue(files) {{
            if (!files.length) return;
            uploadButton.disabled = true;
            stopUploadQueue = false;
            failedUploads = [];
            uploadRetry.hidden = true;
            uploadProgressBox.hidden = false;
            uploadResults.replaceChildren();
            const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
            let completedBytes = 0;
            let completed = 0;
            setUploadControls(true);
            uploadProgress.max = Math.max(totalBytes, 1);
            uploadProgress.value = 0;
            for (const [index, file] of files.entries()) {{
              if (stopUploadQueue) {{
                appendUploadResult(file, '未开始', false);
                failedUploads.push(file);
                continue;
              }}
              uploadStatus.textContent = `正在建立第 ${{index + 1}} / ${{files.length}} 个上传连接：${{file.name}}`;
              const result = await uploadOne(file, index, files.length, completedBytes, totalBytes);
              currentUpload = null;
              if (result.ok) {{
                completed += 1;
                completedBytes += file.size;
                uploadProgress.value = completedBytes;
                appendUploadResult(file, '上传成功', true);
              }} else {{
                failedUploads.push(file);
                appendUploadResult(file, result.status, false);
              }}
            }}
            setUploadControls(false);
            uploadRetry.hidden = failedUploads.length === 0;
            uploadStatus.textContent = `整批完成：${{completed}} 成功 / ${{failedUploads.length}} 失败或未开始。`;
          }}

          uploadForm.addEventListener('submit', event => {{
            event.preventDefault();
            runUploadQueue([...uploadFile.files]);
          }});
          uploadCancel.addEventListener('click', () => {{
            stopUploadQueue = true;
            if (currentUpload) currentUpload.abort();
          }});
          uploadRetry.addEventListener('click', () => runUploadQueue([...failedUploads]));
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
        submitted_batch_id = request.query.getunicode("batch_id") or ""
        batch_id = (
            submitted_batch_id
            if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", submitted_batch_id)
            else ""
        )
        target = (
            "/download/"
            + quote(relative, safe="/")
            + "?download_id="
            + quote(download_id, safe="")
        )
        if batch_id:
            target += "&batch_id=" + quote(batch_id, safe="")
        try:
            lifecycle.register_download(download_id, candidate.stat().st_size)
        except SessionExpiredError:
            return expired_response() or _html_response(_error_page(503, "会话已到期。"), 503)
        return HTTPResponse(
            body=json.dumps(
                {
                    "download_url": target,
                    "status_url": "/download-status/" + quote(download_id, safe=""),
                },
                ensure_ascii=False,
            ),
            status=200,
            content_type="application/json; charset=UTF-8",
        )

    @app.get("/download-status/<download_id>")
    def download_status(download_id: str) -> HTTPResponse:
        expired = expired_response()
        if expired is not None:
            return expired
        client = require_client()
        if isinstance(client, HTTPResponse):
            return client
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", download_id):
            return HTTPResponse(
                body=json.dumps({"status": "missing"}),
                status=404,
                content_type="application/json; charset=UTF-8",
            )
        return HTTPResponse(
            body=json.dumps({"status": lifecycle.download_task_status(download_id)}),
            status=200,
            content_type="application/json; charset=UTF-8",
        )

    @app.post("/pair")
    def pair() -> HTTPResponse:
        if current_client() is not None:
            return redirect("/", code=303)
        expired = expired_response()
        if expired is not None:
            return expired
        remote = request.remote_addr or "unknown"
        if request.content_length < 0 or request.content_length > SMALL_FORM_LIMIT:
            return _html_response(_error_page(413, "配对请求大小无效。"), 413)

        submitted = _pairing_digits(request.forms.getunicode("code") or "")
        device_name = request.forms.getunicode("device_name") or ""
        result, credential = complete_new_client_pairing(
            "code", submitted, device_name, remote
        )
        if result == "limited":
            return _html_response(
                _page("配对受限", "<h1>尝试次数过多</h1><p>请重启服务以重新生成配对码。</p>"),
                429,
            )
        if result == "invalid":
            return _html_response(
                _page(
                    "配对失败",
                    "<h1>配对失败</h1><p>配对码不正确或已被其他设备使用。"
                    "请查看服务机上当前显示的配对码。</p><p><a href=\"/\">返回</a></p>",
                ),
                403,
            )
        if result != "success":
            return _html_response(
                _page(
                    "配对未完成",
                    "<h1>配对未完成</h1><p>会话状态或可信客户机记录发生变化，"
                    "本次请求没有建立信任。请查看服务机上的当前邀请后重试。</p>",
                ),
                503,
            )
        return trusted_cookie_response(credential)

    @app.get("/pair/qr")
    def qr_pairing_landing() -> HTTPResponse | str:
        if current_client() is not None:
            return redirect("/", code=303)
        expired = expired_response()
        if expired is not None:
            return expired
        return _qr_pairing_page()

    @app.post("/pair/qr")
    def qr_pair() -> HTTPResponse:
        if current_client() is not None:
            return redirect("/", code=303)
        expired = expired_response()
        if expired is not None:
            return expired
        if request.content_length < 0 or request.content_length > SMALL_FORM_LIMIT:
            return _html_response(_error_page(413, "二维码配对请求大小无效。"), 413)

        submitted = request.forms.getunicode("token") or ""
        result, credential = complete_new_client_pairing(
            "qr", submitted, "", request.remote_addr or "unknown"
        )
        if result == "invalid":
            return _html_response(
                _page(
                    "邀请已失效",
                    "<main class=\"narrow\"><h1>邀请已使用或失效</h1>"
                    "<p>请重新扫描服务机当前显示的二维码，或使用当前 8 位配对码。</p>"
                    "<p><a href=\"/\">改用配对码</a></p></main>",
                ),
                403,
            )
        if result != "success":
            return _html_response(
                _page(
                    "配对未完成",
                    "<main class=\"narrow\"><h1>配对未完成</h1>"
                    "<p>本次请求没有建立信任，请重新扫描当前二维码。</p></main>",
                ),
                503,
            )
        return trusted_cookie_response(credential)

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
      <p>请在服务机的 LanDrop 窗口或控制台中查看当前 8 位配对码。配对成功后该码会立即更新。</p>
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


def _qr_pairing_page() -> str:
    """Landing page that removes the fragment before submitting its secret."""
    body = """
    <main class="narrow" id="qrStatus">
      <h1>正在连接 LanDrop</h1>
      <p>正在验证这次局域网邀请……</p>
    </main>
    <script>
      const fragment = window.location.hash;
      const qrToken = fragment.startsWith('#') ? fragment.slice(1) : '';
      history.replaceState(null, '', window.location.pathname + window.location.search);
      const qrStatus = document.getElementById('qrStatus');
      async function completeQrPairing() {
        if (!qrToken) {
          qrStatus.innerHTML = '<h1>二维码内容无效</h1><p>请重新扫描服务机当前显示的二维码。</p>';
          return;
        }
        const response = await fetch('/pair/qr', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'},
          body: new URLSearchParams({token: qrToken}).toString()
        });
        if (response.redirected || response.ok) {
          window.location.replace(response.url || '/');
          return;
        }
        document.open();
        document.write(await response.text());
        document.close();
      }
      completeQrPairing().catch(() => {
        qrStatus.innerHTML = '<h1>连接未完成</h1><p>请确认客户机仍与服务机处于同一网络，然后重新扫码。</p>';
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
    body {{ margin: 0; padding-bottom: 88px; background: #f5f7fb; color: #182230; }}
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
    .batch-results {{ display: grid; gap: 6px; padding-left: 20px; margin-bottom: 0; }}
    .batch-results .success {{ color: #087443; }}
    .batch-results .failure {{ color: #b42318; }}
    .files {{ list-style: none; padding: 0; margin: 0; }}
    .files li {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 48px; padding: 8px 0; border-bottom: 1px solid #dfe5ed; }}
    .file-choice {{ display: grid; grid-template-columns: 26px minmax(0, 1fr); align-items: center; flex: 1; min-width: 0; }}
    .file-choice input {{ width: 20px; height: 20px; margin: 0; padding: 0; }}
    .file-link {{ min-width: 0; padding: 8px 4px; overflow-wrap: anywhere; font-weight: 650; }}
    .file-size {{ flex: none; color: #667085; font-size: 13px; }}
    .transfer-status {{ min-height: 24px; margin-bottom: 0; color: #475467; overflow-wrap: anywhere; }}
    .bulk-bar {{ position: fixed; z-index: 10; left: 50%; bottom: 12px; transform: translateX(-50%); display: flex; align-items: center; justify-content: space-between; gap: 12px; width: min(728px, calc(100% - 32px)); padding: 12px 14px; border: 1px solid #d7deea; border-radius: 14px; background: #fff; box-shadow: 0 8px 28px #1822302b; }}
    .bulk-bar span {{ color: #475467; font-size: 14px; }}
    .quiet {{ box-shadow: none; background: transparent; padding: 0; }}
    @media (max-width: 560px) {{
      header, main {{ width: min(100% - 20px, 760px); margin: 14px auto; }}
      section, .narrow {{ padding: 16px; border-radius: 12px; }}
      form {{ align-items: stretch; }}
      form input[type="file"] {{ width: 100%; }}
      form button {{ flex: 1; min-height: 44px; }}
      .file-size {{ max-width: 88px; text-align: right; }}
      .bulk-bar {{ width: calc(100% - 20px); bottom: 8px; }}
    }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #111722; color: #edf3fa; }}
      section, .narrow, .bulk-bar {{ background: #1b2431; }}
      .bulk-bar {{ border-color: #344054; }}
      .file-size, .transfer-status, .bulk-bar span {{ color: #b7c0cf; }}
    }}
  </style>
</head>
<body>{body}</body>
</html>"""
