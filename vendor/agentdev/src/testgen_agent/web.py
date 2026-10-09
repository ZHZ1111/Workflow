"""Local web workbench for running the test-generation agent.

The server deliberately binds to loopback by default.  It serves a small,
dependency-free frontend and delegates every generation run to ``run_agent``;
the web layer does not duplicate controller decisions or test execution.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from pydantic import ValidationError

from .agent import run_agent
from .config import ConfigError, load_config
from .llm_client import DeepSeekClient
from .models import ModelInfo, OracleMode, TaskInput


STATIC_ROOT = Path(__file__).with_name("web_static")
MAX_REQUEST_BYTES = 256 * 1024
MAX_SOURCE_CHARS = 100_000
MAX_SPEC_CHARS = 100_000


@dataclass
class WebJob:
    job_id: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    output_dir: Path | None = None
    error: str | None = None


class JobStore:
    """Small in-memory registry; durable run artifacts remain under outputs/."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._jobs: dict[str, WebJob] = {}
        self._lock = threading.Lock()

    def create(self) -> WebJob:
        job = WebJob(job_id=uuid4().hex[:12])
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> WebJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for name, value in changes.items():
                setattr(job, name, value)


def _safe_error(error: Exception) -> str:
    if isinstance(error, ConfigError):
        return str(error)
    if isinstance(error, ValidationError):
        return "提交内容不符合运行要求，请检查轮次、超时和源码。"
    if isinstance(error, FileExistsError):
        return "运行目录已存在，请重新发起任务。"
    if isinstance(error, (OSError, ValueError)):
        return str(error) or type(error).__name__
    return "运行遇到未预期错误，请查看终端日志。"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_trace(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return events
    return events


def build_bootstrap(project_root: Path) -> dict[str, Any]:
    try:
        config = load_config(env_file=project_root / ".env")
    except ConfigError:
        config_status = {"ready": False, "model": None}
    else:
        config_status = {"ready": True, "model": config.model}
    return {"config": config_status}


def validate_run_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("请求内容必须是 JSON 对象。")
    source = value.get("source")
    spec = value.get("spec", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("请填写 Python 源码。")
    if not isinstance(spec, str):
        raise ValueError("行为规格必须是文本。")
    if len(source) > MAX_SOURCE_CHARS or len(spec) > MAX_SPEC_CHARS:
        raise ValueError("源码或规格过长。")
    if not spec.strip():
        raise ValueError("请填写行为规格。")
    return {
        "source": source,
        "spec": spec,
    }


def _job_snapshot(job: WebJob) -> dict[str, Any]:
    output = job.output_dir
    payload: dict[str, Any] = {
        "id": job.job_id,
        "status": job.status,
        "createdAt": job.created_at,
        "error": job.error,
        "trace": [],
        "state": None,
        "summary": None,
        "testCode": None,
        "reportMarkdown": None,
    }
    if output is None:
        return payload
    payload["trace"] = _read_trace(output / "trace.jsonl")
    payload["state"] = _read_json(output / "state.json")
    payload["summary"] = _read_json(output / "summary.json")
    test_file = output / "test_subject.py"
    if test_file.is_file():
        try:
            payload["testCode"] = test_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            pass
    report_file = output / "report.md"
    if report_file.is_file():
        try:
            payload["reportMarkdown"] = report_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            pass
    return payload


def _run_job(store: JobStore, job_id: str, payload: dict[str, Any]) -> None:
    job_root = store.project_root / "outputs" / "web" / job_id
    input_root = store.project_root / "outputs" / "web-inputs" / job_id
    output_dir = job_root
    store.update(job_id, status="preparing", output_dir=output_dir)
    try:
        input_root.mkdir(parents=True, exist_ok=False)
        source_path = input_root / "subject.py"
        source_path.write_text(payload["source"], encoding="utf-8")
        spec_path = None
        if payload["spec"].strip():
            spec_path = input_root / "specification.md"
            spec_path.write_text(payload["spec"], encoding="utf-8")
        config = load_config(env_file=store.project_root / ".env")
        request = TaskInput(
            source_path=source_path,
            spec_path=spec_path,
            output_dir=output_dir,
            max_rounds=config.max_rounds,
            execution_timeout_seconds=config.execution_timeout_seconds,
            model=ModelInfo.from_config(config),
        )
        store.update(job_id, status="running")
        result = run_agent(
            request,
            client=DeepSeekClient(config),
            oracle_mode=OracleMode.SPECIFICATION,
            behavior_specification=payload["spec"] or None,
        )
    except Exception as error:  # The background boundary must always settle the job.
        store.update(job_id, status="failed", error=_safe_error(error))
        return
    if result.decision.status is not None and result.decision.status.value == "failed":
        store.update(job_id, status="failed", error=result.decision.reason)
    else:
        store.update(job_id, status="completed")


class WebHandler(BaseHTTPRequestHandler):
    server_version = "TestGenWorkbench/0.1"

    @property
    def app(self) -> "WebServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {format % args}")

    def _send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str, content_type: str) -> None:
        path = STATIC_ROOT / name
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            self._send_static("index.html", "text/html; charset=utf-8")
            return
        if path == "/styles.css":
            self._send_static("styles.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._send_static("app.js", "text/javascript; charset=utf-8")
            return
        if path == "/api/bootstrap":
            self._send_json(build_bootstrap(self.app.store.project_root))
            return
        if path.startswith("/api/runs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = self.app.store.get(job_id)
            if job is None:
                self._send_json({"error": "未找到该运行。"}, HTTPStatus.NOT_FOUND)
            else:
                self._send_json(_job_snapshot(job))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path != "/api/runs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json({"error": "请求大小无效。"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            raw = self.rfile.read(length)
            payload = validate_run_payload(json.loads(raw.decode("utf-8")))
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            self._send_json({"error": _safe_error(error)}, HTTPStatus.BAD_REQUEST)
            return
        job = self.app.store.create()
        thread = threading.Thread(
            target=_run_job,
            args=(self.app.store, job.job_id, payload),
            name=f"testgen-web-{job.job_id}",
            daemon=True,
        )
        thread.start()
        self._send_json({"id": job.job_id, "status": job.status}, HTTPStatus.ACCEPTED)


class WebServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], project_root: Path) -> None:
        super().__init__(address, WebHandler)
        self.store = JobStore(project_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 TestGen Agent 本地 Web 工作台。")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只允许本机访问")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path.cwd()
    server = WebServer((args.host, args.port), project_root)
    print(f"TestGen Studio 已启动：http://{args.host}:{server.server_port}")
    print("按 Ctrl+C 停止。真实运行会读取当前目录的 .env 并调用已配置的模型。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nTestGen Studio 已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
