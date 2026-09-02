#!/usr/bin/env python3
"""Local ReportAI application server for the UI, SQLite, and llama-server."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_DATABASE_PATH = Path("reports.db")
DEFAULT_USERNAME = "reportai"
DEFAULT_PASSWORD = "reportai"
LEGACY_USERNAME = "odw0902"
PROMPT_FILES = {
    "supervisor": "supervisor.md",
    "document_writer": "document_writer.md",
    "supervisor_assessment": "supervisor_assessment.md",
    "report_writing": "report_writing.md",
    "report_revision": "report_revision.md",
    "report_review": "report_review.md",
    "supplement_generation": "supplement_generation.md",
    "monthly_plan_generation": "monthly_plan_generation.md",
}


class ReportRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    department TEXT NOT NULL,
                    request TEXT NOT NULL,
                    document TEXT NOT NULL,
                    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
                    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
                    attempts INTEGER NOT NULL CHECK (attempts >= 1),
                    created_at TEXT NOT NULL,
                    owner_username TEXT
                )
                """
            )
            report_columns = {row["name"] for row in connection.execute("PRAGMA table_info(reports)")}
            if "owner_username" not in report_columns:
                connection.execute("ALTER TABLE reports ADD COLUMN owner_username TEXT")
            self._ensure_default_user(connection)
            connection.execute(
                "UPDATE reports SET owner_username = ? WHERE owner_username IS NULL OR owner_username = ''",
                (DEFAULT_USERNAME,),
            )
            connection.execute(
                "UPDATE reports SET owner_username = ? WHERE owner_username = ?",
                (DEFAULT_USERNAME, LEGACY_USERNAME),
            )
            connection.execute("DELETE FROM users WHERE username = ?", (LEGACY_USERNAME,))
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reskilling_supplements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monthly_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16_384, r=8, p=1)

    def _ensure_default_user(self, connection: sqlite3.Connection) -> None:
        salt = secrets.token_bytes(16)
        connection.execute(
            """
            INSERT INTO users (username, password_salt, password_hash) VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET password_salt = excluded.password_salt, password_hash = excluded.password_hash
            """,
            (DEFAULT_USERNAME, salt, self._password_hash(DEFAULT_PASSWORD, salt)),
        )

    def authenticate(self, username: str, password: str) -> bool:
        with self._connect() as connection:
            user = connection.execute(
                "SELECT password_salt, password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        if not user:
            return False
        actual = self._password_hash(password, user["password_salt"])
        return secrets.compare_digest(actual, user["password_hash"])

    def save(self, report: dict[str, Any], owner_username: str) -> dict[str, Any]:
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        values = (
            str(report["title"]).strip() or "제목 미생성",
            str(report.get("category", "")).strip(),
            str(report.get("department", "")).strip(),
            str(report["request"]).strip(),
            str(report["document"]).strip(),
            int(bool(report.get("approved", False))),
            int(report.get("score", 0)),
            max(1, int(report.get("attempts", 1))),
            created_at,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reports
                    (title, category, department, request, document, approved, score, attempts, created_at, owner_username)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values + (owner_username,),
            )
        return {"id": cursor.lastrowid, "created_at": created_at}

    def list_recent(self, limit: int, owner_username: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reports WHERE owner_username = ? ORDER BY id DESC LIMIT ?",
                (owner_username, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) | {"approved": bool(row["approved"])} for row in rows]

    def get(self, report_id: int, owner_username: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reports WHERE id = ? AND owner_username = ?", (report_id, owner_username)
            ).fetchone()
            if not row:
                return None
            supplement = connection.execute(
                "SELECT content, created_at, updated_at FROM reskilling_supplements WHERE report_id = ?", (report_id,)
            ).fetchone()
            monthly_plan = connection.execute(
                "SELECT content, created_at, updated_at FROM monthly_plans WHERE report_id = ?", (report_id,)
            ).fetchone()
        report = dict(row) | {"approved": bool(row["approved"])}
        report["supplement"] = dict(supplement) if supplement else None
        report["monthly_plan"] = dict(monthly_plan) if monthly_plan else None
        return report

    def save_supplement(self, report_id: int, content: str, owner_username: str) -> dict[str, Any] | None:
        content = content.strip()
        if not content:
            raise ValueError("보충자료 내용은 필수입니다.")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM reports WHERE id = ? AND owner_username = ?", (report_id, owner_username)
            ).fetchone():
                return None
            connection.execute(
                """
                INSERT INTO reskilling_supplements (report_id, content, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at
                """,
                (report_id, content, now, now),
            )
            supplement = connection.execute(
                "SELECT content, created_at, updated_at FROM reskilling_supplements WHERE report_id = ?", (report_id,)
            ).fetchone()
        return dict(supplement)

    def save_monthly_plan(self, report_id: int, content: str, owner_username: str) -> dict[str, Any] | None:
        content = content.strip()
        if not content:
            raise ValueError("월간업무계획 내용은 필수입니다.")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM reports WHERE id = ? AND owner_username = ?", (report_id, owner_username)
            ).fetchone():
                return None
            connection.execute(
                """
                INSERT INTO monthly_plans (report_id, content, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at
                """,
                (report_id, content, now, now),
            )
            monthly_plan = connection.execute(
                "SELECT content, created_at, updated_at FROM monthly_plans WHERE report_id = ?", (report_id,)
            ).fetchone()
        return dict(monthly_plan)

    def update(self, report_id: int, report: dict[str, Any], owner_username: str) -> bool:
        values = (
            str(report["title"]).strip() or "제목 미생성",
            str(report.get("category", "")).strip(),
            str(report.get("department", "")).strip(),
            str(report["request"]).strip(),
            str(report["document"]).strip(),
            int(bool(report.get("approved", False))),
            int(report.get("score", 0)),
            max(1, int(report.get("attempts", 1))),
            report_id, owner_username,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reports SET title = ?, category = ?, department = ?, request = ?, document = ?,
                    approved = ?, score = ?, attempts = ?
                WHERE id = ? AND owner_username = ?
                """,
                values,
            )
        return cursor.rowcount == 1

    def delete(self, report_id: int, owner_username: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM reports WHERE id = ? AND owner_username = ?", (report_id, owner_username)
            )
        return cursor.rowcount == 1


class ReportStorageHandler(BaseHTTPRequestHandler):
    repository: ReportRepository
    frontend_path: Path
    prompts_path: Path
    llama_base_url: str
    sessions: dict[str, tuple[str, float]]
    secure_cookie: bool

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_POST(self) -> None:
        path = self._path()
        if path == "/api/auth/login":
            self._login()
            return
        username = self._require_auth()
        if not username:
            return
        if path == "/api/auth/logout":
            self._logout()
            return
        if path == "/api/llm/chat/completions":
            self._proxy_llama("POST", "/chat/completions")
            return
        path = self._reports_path(path)
        if path is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        if path == "/reports":
            try:
                payload = self._read_report()
                saved = self.repository.save(payload, username)
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._send_json(HTTPStatus.CREATED, saved)
            return
        report_id = self._supplement_report_id(path)
        if report_id is not None:
            try:
                supplement = self.repository.save_supplement(report_id, self._read_content(), username)
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            if supplement is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "보고서를 찾을 수 없습니다."})
                return
            self._send_json(HTTPStatus.CREATED, supplement)
            return
        report_id = self._monthly_plan_report_id(path)
        if report_id is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            monthly_plan = self.repository.save_monthly_plan(report_id, self._read_content(), username)
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if monthly_plan is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "보고서를 찾을 수 없습니다."})
            return
        self._send_json(HTTPStatus.CREATED, monthly_plan)

    def do_GET(self) -> None:
        path = self._path()
        if path in {"/", "/report_agent.html"}:
            self._send_frontend()
            return
        if path == "/api/auth/me":
            username = self._session_username()
            if not username:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "로그인이 필요합니다."})
                return
            self._send_json(HTTPStatus.OK, {"username": username})
            return
        username = self._require_auth()
        if not username:
            return
        if path == "/api/llm/models":
            self._proxy_llama("GET", "/models")
            return
        if path == "/api/prompts":
            self._send_prompts()
            return
        path = self._reports_path(path)
        if path is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        if path == "/reports":
            self._send_json(HTTPStatus.OK, {"reports": self.repository.list_recent(20, username)})
            return
        report_id = self._report_id(path)
        if report_id is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        report = self.repository.get(report_id, username)
        if not report:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "보고서를 찾을 수 없습니다."})
            return
        self._send_json(HTTPStatus.OK, report)

    def do_PUT(self) -> None:
        username = self._require_auth()
        if not username:
            return
        prompt_name = self._prompt_name(self._path())
        if prompt_name is not None:
            try:
                content = self._read_content()
                if not content.strip():
                    raise ValueError("프롬프트 내용은 비워둘 수 없습니다.")
                self._prompt_path(prompt_name).write_text(content.rstrip() + "\n", encoding="utf-8")
            except (ValueError, json.JSONDecodeError, OSError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._send_json(HTTPStatus.OK, {"name": prompt_name, "content": content.rstrip() + "\n"})
            return
        path = self._reports_path(self._path())
        report_id = self._report_id(path) if path else None
        if report_id is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            updated = self.repository.update(report_id, self._read_report(), username)
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if not updated:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "보고서를 찾을 수 없습니다."})
            return
        self._send_json(HTTPStatus.OK, {"id": report_id})

    def do_DELETE(self) -> None:
        username = self._require_auth()
        if not username:
            return
        path = self._reports_path(self._path())
        report_id = self._report_id(path) if path else None
        if report_id is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        if not self.repository.delete(report_id, username):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "보고서를 찾을 수 없습니다."})
            return
        self._send_json(HTTPStatus.OK, {"id": report_id})

    def _read_report(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(payload, dict) or not all(payload.get(key) for key in ("title", "request", "document")):
            raise ValueError("title, request, document는 필수입니다.")
        return payload

    def _read_content(self) -> str:
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("요청 형식이 올바르지 않습니다.")
        return str(payload.get("content", ""))

    def _login(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
        except (ValueError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "로그인 요청 형식이 올바르지 않습니다."})
            return
        if not username or not password or not self.repository.authenticate(username, password):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "아이디 또는 비밀번호가 올바르지 않습니다."})
            return
        token = secrets.token_urlsafe(32)
        self.sessions[token] = (username, time.time() + 28_800)
        cookie = f"reportai_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=28800"
        if self.secure_cookie:
            cookie += "; Secure"
        self._send_json(HTTPStatus.OK, {"username": username}, cookie)

    def _logout(self) -> None:
        token = self._session_token()
        if token:
            self.sessions.pop(token, None)
        self._send_json(
            HTTPStatus.OK,
            {"message": "로그아웃했습니다."},
            "reportai_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0",
        )

    def _require_auth(self) -> str | None:
        username = self._session_username()
        if not username:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "로그인이 필요합니다."})
        return username

    def _session_username(self) -> str | None:
        token = self._session_token()
        if not token:
            return None
        session = self.sessions.get(token)
        if not session or session[1] < time.time():
            self.sessions.pop(token, None)
            return None
        return session[0]

    def _session_token(self) -> str | None:
        for item in self.headers.get("Cookie", "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == "reportai_session":
                return value
        return None

    def _path(self) -> str:
        return urlparse(self.path).path

    def _send_prompts(self) -> None:
        try:
            prompts = {
                name: self._prompt_path(name).read_text(encoding="utf-8")
                for name in PROMPT_FILES
            }
        except OSError as error:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"프롬프트 파일을 읽지 못했습니다: {error}"})
            return
        self._send_json(HTTPStatus.OK, {"prompts": prompts})

    def _prompt_path(self, name: str) -> Path:
        return self.prompts_path / PROMPT_FILES[name]

    @staticmethod
    def _prompt_name(path: str) -> str | None:
        match = __import__("re").fullmatch(r"/api/prompts/([a-z_]+)", path)
        if not match:
            return None
        name = match.group(1)
        return name if name in PROMPT_FILES else None

    @staticmethod
    def _reports_path(path: str) -> str | None:
        if path.startswith("/api/reports"):
            return path.removeprefix("/api")
        return None

    def _send_frontend(self) -> None:
        try:
            body = self.frontend_path.read_bytes()
        except OSError as error:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"화면 파일을 읽지 못했습니다: {error}"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_llama(self, method: str, path: str) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size) if size else None
        request = urllib.request.Request(
            f"{self.llama_base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"} if body else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                content_type = response.headers.get("Content-Type", "application/json; charset=utf-8")
                self.send_response(response.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                if content_type.startswith("text/event-stream"):
                    while line := response.readline():
                        self.wfile.write(line)
                        self.wfile.flush()
                else:
                    while chunk := response.read(8192):
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            self._send_json(HTTPStatus(error.code), {"error": detail})
        except urllib.error.URLError as error:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": f"llama-server 연결 실패: {error.reason}"})

    @staticmethod
    def _report_id(path: str) -> int | None:
        match = __import__("re").fullmatch(r"/reports/(\d+)", path)
        return int(match.group(1)) if match else None

    @staticmethod
    def _supplement_report_id(path: str) -> int | None:
        match = __import__("re").fullmatch(r"/reports/(\d+)/supplement", path)
        return int(match.group(1)) if match else None

    @staticmethod
    def _monthly_plan_report_id(path: str) -> int | None:
        match = __import__("re").fullmatch(r"/reports/(\d+)/monthly-plan", path)
        return int(match.group(1)) if match else None

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any], cookie: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="보고서 SQLite 저장 서버")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--llama-url", default="http://127.0.0.1:8080/v1", help="llama-server OpenAI 호환 API 주소")
    parser.add_argument("--secure-cookie", action="store_true", help="HTTPS 환경에서 Secure 세션 쿠키 사용")
    args = parser.parse_args()

    ReportStorageHandler.repository = ReportRepository(args.database)
    ReportStorageHandler.frontend_path = Path(__file__).with_name("report_agent.html")
    ReportStorageHandler.prompts_path = Path(__file__).with_name("agent")
    ReportStorageHandler.llama_base_url = args.llama_url.rstrip("/")
    ReportStorageHandler.sessions = {}
    ReportStorageHandler.secure_cookie = args.secure_cookie
    server = ThreadingHTTPServer((args.host, args.port), ReportStorageHandler)
    print(f"ReportAI 실행: http://{args.host}:{args.port} (DB: {args.database}, LLM: {ReportStorageHandler.llama_base_url})")
    server.serve_forever()


if __name__ == "__main__":
    main()