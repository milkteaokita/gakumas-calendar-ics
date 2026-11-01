import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from server import CalendarService, make_handler


SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:test-evt-1@gakusei-cheer-portal.web.app
DTSTART:20991201T100000Z
DTEND:20991201T110000Z
SUMMARY:テスト生放送
DESCRIPTION:出演者: 花岩 香奈、飯田 ヒカル\\n出典: https://gakusei-cheer-portal.web.app/calendar
END:VEVENT
END:VCALENDAR
"""


class ServerTests(unittest.TestCase):
    def test_server_endpoints_and_upload(self):
        app_root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "calendar.ics").write_text(SAMPLE_ICS, encoding="utf-8")

            service = CalendarService(
                app_dir=app_root,
                data_dir=data_dir,
                enable_playwright_fetch=False,
                admin_token="secret123",
            )
            service.initialize_data_dir()

            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{port}"
            try:
                # 1. Healthcheck
                with urlopen(f"{base}/healthz") as res:
                    self.assertEqual(res.status, 200)
                    self.assertEqual(json.loads(res.read().decode())["status"], "ok")

                # 2. Index HTML (bilingual zh-TW / zh-CN) & Admin HTML (/admin)
                with urlopen(f"{base}/") as res:
                    html = res.read().decode("utf-8")
                    self.assertIn("繁體中文", html)
                    self.assertIn("简体中文", html)

                with urlopen(f"{base}/admin") as res:
                    admin_html = res.read().decode("utf-8")
                    self.assertIn("Retrigger", admin_html)

                with urlopen(f"{base}/api/logs") as res:
                    logs_payload = json.loads(res.read().decode("utf-8"))
                    self.assertIn("logs", logs_payload)
                    self.assertTrue(len(logs_payload["logs"]) >= 1)

                # 3. Casts HTML & JSON
                with urlopen(f"{base}/casts/index.json") as res:
                    manifest = json.loads(res.read().decode("utf-8"))
                    self.assertEqual(len(manifest["casts"]), 2)
                    self.assertTrue(manifest["casts"][0]["url"].startswith(base))

                # 4. Upload requires admin token when configured
                req_unauth = Request(
                    f"{base}/api/upload",
                    data=json.dumps({"ics": SAMPLE_ICS}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(req_unauth)
                self.assertEqual(ctx.exception.code, 401)

                # 5. Upload with valid token succeeds
                req_auth = Request(
                    f"{base}/api/upload",
                    data=json.dumps({"ics": SAMPLE_ICS}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Admin-Token": "secret123"},
                    method="POST",
                )
                with urlopen(req_auth) as res:
                    result = json.loads(res.read().decode("utf-8"))
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["cast_count"], 2)
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
