#!/usr/bin/env python3
"""Portfolio contact backend + static file server.

Run from this folder:
  python3 server.py

Then open http://127.0.0.1:8787

POST /api/contact  JSON: name, email, subject, message
Saves every valid message to data/submissions.jsonl
Emails amrithul007@gmail.com when SMTP env vars are set:

  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=you@gmail.com
  SMTP_PASS=your-app-password
  CONTACT_TO=amrithul007@gmail.com
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STORE = DATA_DIR / "submissions.jsonl"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
CONTACT_TO = os.environ.get("CONTACT_TO", "amrithul007@gmail.com")
MAX_LEN = {
    "name": 80,
    "email": 120,
    "subject": 140,
    "message": 4000,
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HITS: dict[str, list[float]] = {}


def json_bytes(payload: dict, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode("utf-8")


def rate_ok(ip: str, limit: int = 8, window: float = 600.0) -> bool:
    now = time.time()
    bucket = [t for t in HITS.get(ip, []) if now - t < window]
    if len(bucket) >= limit:
        HITS[ip] = bucket
        return False
    bucket.append(now)
    HITS[ip] = bucket
    return True


def clean(value: object, field: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text[: MAX_LEN[field]]


def validate(body: dict) -> tuple[dict | None, str | None]:
    if body.get("website"):
        return None, "ignored"
    data = {
        "name": clean(body.get("name"), "name"),
        "email": clean(body.get("email"), "email"),
        "subject": clean(body.get("subject"), "subject"),
        "message": clean(body.get("message"), "message"),
    }
    if not all(data.values()):
        return None, "Please fill in every field."
    if not EMAIL_RE.match(data["email"]):
        return None, "That email address does not look valid."
    return data, None


def persist(entry: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with STORE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def send_mail(entry: dict) -> str:
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    if not user or not password:
        return "saved"

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    mail = EmailMessage()
    mail["Subject"] = f"[Portfolio] {entry['subject']}"
    mail["From"] = user
    mail["To"] = CONTACT_TO
    mail["Reply-To"] = entry["email"]
    mail.set_content(
        f"Name: {entry['name']}\n"
        f"Email: {entry['email']}\n"
        f"Subject: {entry['subject']}\n"
        f"Time: {entry['received_at']}\n\n"
        f"{entry['message']}\n"
    )
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls(context=context)
        smtp.login(user, password)
        smtp.send_message(mail)
    return "sent"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _write(self, status: int, payload: dict):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        if self.path.rstrip("/") == "/api/contact":
            self._write(HTTPStatus.NO_CONTENT, {"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path.rstrip("/") != "/api/contact":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        ip = self.client_address[0]
        if not rate_ok(ip):
            self._write(HTTPStatus.TOO_MANY_REQUESTS, {
                "ok": False,
                "error": "Too many messages. Try again in a few minutes.",
            })
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > 20_000:
            self._write(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
                "ok": False,
                "error": "Message is too large.",
            })
            return

        raw = self.rfile.read(length) if length else b"{}"
        ctype = (self.headers.get("Content-Type") or "").lower()
        try:
            if "application/json" in ctype:
                body = json.loads(raw.decode("utf-8") or "{}")
            else:
                parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                body = {key: values[-1] for key, values in parsed.items()}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "error": "Could not read that request.",
            })
            return

        data, error = validate(body if isinstance(body, dict) else {})
        if error == "ignored":
            self._write(HTTPStatus.OK, {"ok": True})
            return
        if error:
            self._write(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
            return

        entry = {
            **data,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "ip": ip,
        }
        try:
            persist(entry)
        except OSError:
            self._write(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "error": "Could not save the message.",
            })
            return

        delivery = "saved"
        try:
            delivery = send_mail(entry)
        except Exception as exc:
            delivery = "saved"
            print("mail send failed:", exc)

        self._write(HTTPStatus.OK, {
            "ok": True,
            "delivery": delivery,
            "message": "Message received. I will get back to you soon.",
        })

    def log_message(self, fmt: str, *args):
        print(self.log_date_time_string(), "-", fmt % args)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Rithul portfolio running on http://{HOST}:{PORT}")
    print("Contact endpoint: POST /api/contact")
    if not os.environ.get("SMTP_USER"):
        print("SMTP not set — messages are saved to data/submissions.jsonl")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
