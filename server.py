#!/usr/bin/env python3
"""Linux Docker Web Server & Background Scheduler for Gakumas Calendar ICS.

Serves:
  - /                  Public bilingual (zh-TW / zh-CN) subscription portal
  - /admin             Backend administration dashboard (retrigger, upload, live Playwright logs)
  - /calendar.ics      Full Gakumas voice actor schedule feed
  - /casts/            Bilingual voice actor table index
  - /casts/index.json  JSON manifest of per-actor ICS feeds
  - /casts/cast-*.ics  Individual voice actor ICS feeds
  - /api/status        JSON server & sync status
  - /api/logs          JSON server status + recent Playwright/sync log lines
  - /api/logs/clear    POST endpoint to clear logs
  - /api/refresh       POST endpoint to retrigger sync/split from /admin
  - /api/upload        POST endpoint to upload a new calendar.ics and re-split from /admin
  - /healthz           Docker healthcheck endpoint
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
from urllib.parse import urlparse

from split_casts import CHARACTER_MAP, JST, read_calendar, split as split_casts_dir
from update_calendar import fetch_events, serialize_events

UTC = timezone.utc
CAST_FILE_RE = re.compile(r"^cast-[0-9a-f]{16}\.ics$")
MAX_LOG_LINES = 600


def env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class CalendarService:
    """Manages persistent calendar data, splitting, logs, and scheduled updates."""

    def __init__(
        self,
        *,
        app_dir: Path,
        data_dir: Path,
        base_url: str = "",
        enable_playwright_fetch: bool = False,
        fetch_months: int = 18,
        refresh_interval_hours: float = 3.0,
        admin_token: str = "",
    ) -> None:
        self.app_dir = app_dir.resolve()
        self.data_dir = data_dir.resolve()
        self.base_url = base_url.rstrip("/")
        self.enable_playwright_fetch = enable_playwright_fetch
        self.fetch_months = fetch_months
        self.refresh_interval_hours = max(0.25, float(refresh_interval_hours))
        self.admin_token = admin_token.strip()

        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self._logs: deque[str] = deque(maxlen=MAX_LOG_LINES)

        self.sync_state: dict[str, object] = {
            "status": "idle",
            "current_step": "系統已啟動，待機中 / System ready",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
            "next_run_at": None,
            "interval_hours": self.refresh_interval_hours,
            "playwright_enabled": self.enable_playwright_fetch,
            "auth_required": bool(self.admin_token),
        }

    @property
    def calendar_path(self) -> Path:
        return self.data_dir / "calendar.ics"

    @property
    def casts_dir(self) -> Path:
        return self.data_dir / "casts"

    @property
    def log_file_path(self) -> Path:
        return self.data_dir / "sync.log"

    def append_log(self, message: str, *, level: str = "INFO") -> None:
        ts_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        line = f"[{ts_jst}] [{level}] {message}"
        print(line, flush=True)
        with self._log_lock:
            self._logs.append(line)
            self.sync_state["current_step"] = message
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                with self.log_file_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def clear_logs(self) -> None:
        with self._log_lock:
            self._logs.clear()
            try:
                if self.log_file_path.exists():
                    self.log_file_path.write_text("", encoding="utf-8")
            except Exception:
                pass
        self.append_log("管理員已清空日誌 / Logs cleared by admin")

    def get_logs(self) -> list[str]:
        with self._log_lock:
            return list(self._logs)

    def initialize_data_dir(self) -> None:
        """Seed /data from /app if needed and ensure casts/ is freshly split."""
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Load existing log tail if present
        if self.log_file_path.is_file():
            try:
                existing = self.log_file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                with self._log_lock:
                    for ln in existing[-MAX_LOG_LINES:]:
                        self._logs.append(ln)
            except Exception:
                pass

        seed_cal = self.app_dir / "calendar.ics"
        if not self.calendar_path.exists() and seed_cal.exists():
            shutil.copy2(seed_cal, self.calendar_path)
            self.append_log(f"已將初始 calendar.ics 複製至資料目錄 ({self.calendar_path})")

        seed_casts = self.app_dir / "casts"
        if not (self.casts_dir / "index.json").exists() and seed_casts.is_dir():
            self.casts_dir.mkdir(parents=True, exist_ok=True)
            for item in seed_casts.iterdir():
                if item.is_file():
                    shutil.copy2(item, self.casts_dir / item.name)
            self.append_log("已初始化 casts/ 聲優日曆目錄")

        if self.calendar_path.exists():
            try:
                self.append_log("正在依今日 (JST) 日期驗證並拆分現有 calendar.ics...")
                manifest = split_casts_dir(self.calendar_path, self.casts_dir, base_url=self.base_url)
                self.sync_state["status"] = "ok"
                self.sync_state["last_success_at"] = datetime.now(UTC).isoformat()
                self.append_log(f"啟動拆分完成：共 {len(manifest)} 位聲優獨立日曆已就緒")
            except Exception as exc:
                self.append_log(f"啟動時拆分警告: {exc}", level="WARN")

    def check_auth(self, token: str | None) -> bool:
        if not self.admin_token:
            return True
        return bool(token and token.strip() == self.admin_token)

    def run_sync(self, *, fetch_remote: bool = True) -> bool:
        """Fetch latest schedule (if enabled) and re-split voice actor feeds."""
        if not self._lock.acquire(blocking=False):
            self.append_log("已有同步任務正在執行中，略過本次重複觸發", level="WARN")
            return False
        try:
            now_iso = datetime.now(UTC).isoformat()
            self.sync_state["status"] = "running"
            self.sync_state["last_attempt_at"] = now_iso
            fetch_err: str | None = None

            mode_desc = "Playwright 遠端抓取 + 聲優拆分" if (fetch_remote and self.enable_playwright_fetch) else "僅重新拆分現有 calendar.ics"
            self.append_log(f"=== 開始執行同步任務 ({mode_desc}) ===")

            if fetch_remote and self.enable_playwright_fetch:
                try:
                    events = fetch_events(
                        visible=False,
                        months=self.fetch_months,
                        logger=lambda msg: self.append_log(msg),
                    )
                    self.append_log(f"[Serialize] 正在將 {len(events)} 筆原始資料轉換為 RFC 5545 ICS 格式...")
                    data, count = serialize_events(events)
                    if count == 0:
                        raise RuntimeError("No today/future events found; keeping existing calendar.ics")
                    if self.calendar_path.exists():
                        old_text = self.calendar_path.read_text(encoding="utf-8", errors="ignore")
                        old_count = old_text.count("BEGIN:VEVENT")
                        if old_count >= 12 and count < old_count * 0.45:
                            raise RuntimeError(
                                f"Event count dropped from {old_count} to {count}; refusing replacement"
                            )
                    tmp = self.calendar_path.with_suffix(".ics.tmp")
                    tmp.write_bytes(data)
                    tmp.replace(self.calendar_path)
                    self.append_log(f"[Serialize] 成功寫入 calendar.ics (有效未來行程: {count} 筆, {len(data):,} bytes)")
                except Exception as exc:
                    fetch_err = str(exc)
                    self.append_log(
                        f"[Playwright] 遠端抓取失敗 (將保留現有 calendar.ics 並繼續拆分): {exc}",
                        level="ERROR",
                    )

            # Always re-split existing calendar.ics so today's date filtering in JST stays fresh
            if self.calendar_path.exists():
                self.append_log("[Split] 正在依『出演者』拆分各聲優獨立 ICS...")
                manifest = split_casts_dir(self.calendar_path, self.casts_dir, base_url=self.base_url)
                total_links = sum(x["count"] for x in manifest)
                self.append_log(f"[Split] 拆分完成！共產生 {len(manifest)} 位聲優日曆 (合計 {total_links} 筆關聯行程)")

            if fetch_err:
                self.sync_state["status"] = "error"
                self.sync_state["last_error"] = fetch_err
                self.sync_state["current_step"] = f"上次遠端抓取失敗 (已保留舊檔): {fetch_err}"
                return False

            self.sync_state["status"] = "ok"
            self.sync_state["last_success_at"] = datetime.now(UTC).isoformat()
            self.sync_state["last_error"] = None
            self.append_log("=== 同步任務全部完成 (OK) ===")
            return True
        except Exception as exc:
            self.sync_state["status"] = "error"
            self.sync_state["last_error"] = str(exc)
            self.append_log(f"=== 同步任務發生例外錯誤: {exc} ===", level="ERROR")
            return False
        finally:
            self._lock.release()

    def trigger_background_sync(self, *, fetch_remote: bool = True) -> bool:
        if self._lock.locked():
            return False
        t = threading.Thread(
            target=self.run_sync,
            kwargs={"fetch_remote": fetch_remote},
            daemon=True,
        )
        t.start()
        return True

    def upload_ics(self, ics_text: str) -> list[dict]:
        """Validate uploaded VCALENDAR text, save atomically, and split casts."""
        if not ics_text or "BEGIN:VCALENDAR" not in ics_text:
            raise ValueError("上傳內容不是有效的 ICS 日曆格式 (缺少 BEGIN:VCALENDAR)")

        with self._lock:
            self.append_log("[Upload] 收到手動上傳的 calendar.ics，正在驗證格式...")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".upload_", suffix=".ics", dir=self.data_dir)
            os.close(fd)
            temp_path = Path(temp_name)
            try:
                temp_path.write_text(ics_text, encoding="utf-8")
                _, events, _ = read_calendar(temp_path)
                if not events:
                    raise ValueError("上傳的 ICS 不包含任何 VEVENT 行程")
                temp_path.replace(self.calendar_path)
                self.append_log(f"[Upload] 驗證通過 (含 {len(events)} 筆 VEVENT)，正在重新拆分聲優日曆...")
                manifest = split_casts_dir(self.calendar_path, self.casts_dir, base_url=self.base_url)
                self.sync_state["status"] = "ok"
                self.sync_state["last_success_at"] = datetime.now(UTC).isoformat()
                self.sync_state["last_error"] = None
                self.append_log(f"[Upload] 手動上傳與拆分完成！共更新 {len(manifest)} 位聲優日曆")
                return manifest
            finally:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)

    def get_manifest(self, request_base_url: str = "") -> dict:
        index_file = self.casts_dir / "index.json"
        if not index_file.exists():
            return {
                "source": "calendar.ics",
                "generated_for_date_jst": datetime.now(JST).date().isoformat(),
                "source_event_count": 0,
                "excluded_expired": 0,
                "excluded_without_cast": 0,
                "casts": [],
            }
        data = json.loads(index_file.read_text(encoding="utf-8"))
        effective_base = (self.base_url or request_base_url).rstrip("/")
        for item in data.get("casts", []):
            name = item.get("name", "")
            meta = CHARACTER_MAP.get(name)
            if meta:
                item.setdefault("character_tw", meta["tw"])
                item.setdefault("character_cn", meta["cn"])
                item.setdefault("color", meta["color"])
            if effective_base and item.get("file"):
                item["url"] = f"{effective_base}/casts/{item['file']}"
        return data

    def get_status_payload(self, request_base_url: str = "") -> dict:
        cal_exists = self.calendar_path.exists()
        cal_mtime = None
        cal_size = 0
        if cal_exists:
            stat = self.calendar_path.stat()
            cal_mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            cal_size = stat.st_size
        return {
            "ok": True,
            "server_time_utc": datetime.now(UTC).isoformat(),
            "today_jst": datetime.now(JST).date().isoformat(),
            "calendar_exists": cal_exists,
            "calendar_mtime": cal_mtime,
            "calendar_size": cal_size,
            "sync": dict(self.sync_state),
            "manifest": self.get_manifest(request_base_url=request_base_url),
        }

    def start_scheduler(self, *, run_on_start: bool = False) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return

        def loop() -> None:
            self.append_log(
                f"背景定時排程器已啟動：每 {self.refresh_interval_hours:g} 小時自動更新一次 "
                f"(REFRESH_ON_START={run_on_start})"
            )
            if run_on_start:
                if self._stop_event.wait(3.0):
                    return
                self.run_sync(fetch_remote=True)
            while not self._stop_event.is_set():
                next_dt = datetime.now(UTC) + timedelta(hours=self.refresh_interval_hours)
                self.sync_state["next_run_at"] = next_dt.isoformat()
                if self._stop_event.wait(self.refresh_interval_hours * 3600.0):
                    break
                self.run_sync(fetch_remote=True)

        self._scheduler_thread = threading.Thread(target=loop, daemon=True)
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        self._stop_event.set()


def make_handler(service: CalendarService):
    class CalendarHTTPRequestHandler(BaseHTTPRequestHandler):
        server_version = "GakumasCalendarDocker/1.1"

        def _request_base_url(self) -> str:
            proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip() or "http"
            host = (
                self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host")
                or "localhost"
            ).split(",")[0].strip()
            return f"{proto}://{host}"

        def _extract_token(self, body_obj: dict | None = None) -> str | None:
            hdr = self.headers.get("X-Admin-Token")
            if hdr:
                return hdr
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            if isinstance(body_obj, dict) and isinstance(body_obj.get("token"), str):
                return body_obj["token"]
            return None

        def _send_bytes(
            self,
            status: int,
            content: bytes,
            content_type: str,
            *,
            extra_headers: dict[str, str] | None = None,
            head_only: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "*")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            if not head_only:
                self.wfile.write(content)

        def _send_json(self, status: int, payload: dict, *, head_only: bool = False) -> None:
            raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            self._send_bytes(status, raw, "application/json; charset=utf-8", head_only=head_only)

        def do_HEAD(self) -> None:
            self._handle_get(head_only=True)

        def do_GET(self) -> None:
            self._handle_get(head_only=False)

        def _handle_get(self, *, head_only: bool = False) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/healthz":
                self._send_json(HTTPStatus.OK, {"status": "ok"}, head_only=head_only)
                return

            if path in ("/", "/index.html"):
                index_path = service.app_dir / "index.html"
                if not index_path.exists():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "index.html not found"}, head_only=head_only)
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    index_path.read_bytes(),
                    "text/html; charset=utf-8",
                    head_only=head_only,
                )
                return

            if path in ("/admin", "/admin/", "/admin/index.html"):
                admin_path = service.app_dir / "admin.html"
                if not admin_path.exists():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "admin.html not found"}, head_only=head_only)
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    admin_path.read_bytes(),
                    "text/html; charset=utf-8",
                    head_only=head_only,
                )
                return

            if path == "/calendar.ics":
                if not service.calendar_path.exists():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "calendar.ics not found"}, head_only=head_only)
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    service.calendar_path.read_bytes(),
                    "text/calendar; charset=utf-8",
                    extra_headers={"Content-Disposition": 'inline; filename="calendar.ics"'},
                    head_only=head_only,
                )
                return

            if path == "/api/status":
                payload = service.get_status_payload(request_base_url=self._request_base_url())
                self._send_json(HTTPStatus.OK, payload, head_only=head_only)
                return

            if path == "/api/logs":
                payload = service.get_status_payload(request_base_url=self._request_base_url())
                payload["logs"] = service.get_logs()
                self._send_json(HTTPStatus.OK, payload, head_only=head_only)
                return

            if path == "/casts":
                self.send_response(HTTPStatus.MovedPermanently)
                self.send_header("Location", "/casts/")
                self.end_headers()
                return

            if path in ("/casts/", "/casts/index.html"):
                casts_html = service.casts_dir / "index.html"
                if not casts_html.exists():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "casts/index.html not found"}, head_only=head_only)
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    casts_html.read_bytes(),
                    "text/html; charset=utf-8",
                    head_only=head_only,
                )
                return

            if path == "/casts/index.json":
                manifest = service.get_manifest(request_base_url=self._request_base_url())
                self._send_json(HTTPStatus.OK, manifest, head_only=head_only)
                return

            if path.startswith("/casts/"):
                filename = path[len("/casts/"):]
                if not CAST_FILE_RE.fullmatch(filename):
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Cast calendar not found"}, head_only=head_only)
                    return
                target = service.casts_dir / filename
                if not target.is_file():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Cast calendar not found"}, head_only=head_only)
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    target.read_bytes(),
                    "text/calendar; charset=utf-8",
                    extra_headers={"Content-Disposition": f'inline; filename="{filename}"'},
                    head_only=head_only,
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"}, head_only=head_only)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 15 * 1024 * 1024:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Payload too large"})
                return
            raw_body = self.rfile.read(length) if length > 0 else b""
            body_obj = None
            if raw_body:
                try:
                    body_obj = json.loads(raw_body.decode("utf-8"))
                except Exception:
                    body_obj = None

            if path == "/api/refresh":
                if not service.check_auth(self._extract_token(body_obj)):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "管理金鑰錯誤或未提供 / Invalid admin token"})
                    return
                fetch_remote = True
                if isinstance(body_obj, dict) and "fetch_remote" in body_obj:
                    fetch_remote = bool(body_obj["fetch_remote"])
                started = service.trigger_background_sync(fetch_remote=fetch_remote)
                self._send_json(
                    HTTPStatus.ACCEPTED if started else HTTPStatus.OK,
                    {
                        "ok": True,
                        "started": started,
                        "fetch_remote": fetch_remote,
                        "message": "Background sync started" if started else "Sync already running",
                    },
                )
                return

            if path == "/api/logs/clear":
                if not service.check_auth(self._extract_token(body_obj)):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "管理金鑰錯誤或未提供 / Invalid admin token"})
                    return
                service.clear_logs()
                self._send_json(HTTPStatus.OK, {"ok": True})
                return

            if path == "/api/upload":
                if not service.check_auth(self._extract_token(body_obj)):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "管理金鑰錯誤或未提供 / Invalid admin token"})
                    return
                if isinstance(body_obj, dict) and isinstance(body_obj.get("ics"), str):
                    ics_text = body_obj["ics"]
                else:
                    ics_text = raw_body.decode("utf-8-sig", errors="replace")
                try:
                    manifest = service.upload_ics(ics_text)
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "cast_count": len(manifest),
                            "message": "Uploaded and split successfully",
                        },
                    )
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception as exc:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})

    return CalendarHTTPRequestHandler


def main() -> int:
    default_data_dir = "/data" if Path("/data").exists() else os.environ.get("DATA_DIR", ".")
    parser = argparse.ArgumentParser(description="Run Gakumas Calendar ICS Docker Server")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3002")))
    parser.add_argument("--data-dir", type=Path, default=Path(default_data_dir))
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", ""))
    args = parser.parse_args()

    app_dir = Path(__file__).resolve().parent
    auto_refresh = env_bool("AUTO_REFRESH", True)
    refresh_on_start = env_bool("REFRESH_ON_START", False)
    enable_playwright = env_bool("ENABLE_PLAYWRIGHT_FETCH", False)
    fetch_months = int(os.environ.get("FETCH_MONTHS", "18"))
    interval_hours = float(os.environ.get("REFRESH_INTERVAL_HOURS", "3"))
    admin_token = os.environ.get("ADMIN_TOKEN", "")

    service = CalendarService(
        app_dir=app_dir,
        data_dir=args.data_dir,
        base_url=args.base_url,
        enable_playwright_fetch=enable_playwright,
        fetch_months=fetch_months,
        refresh_interval_hours=interval_hours,
        admin_token=admin_token,
    )
    service.initialize_data_dir()
    if auto_refresh:
        service.start_scheduler(run_on_start=refresh_on_start)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    service.append_log(
        f"HTTP 伺服器已啟動: http://{args.host}:{args.port} "
        f"(後台: /admin, 資料目錄: {service.data_dir})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_scheduler()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
