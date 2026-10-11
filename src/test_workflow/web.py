"""Loopback workbench backed by the same application service as the CLI."""

from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from testgen_agent.config import load_config
from testgen_agent.llm_client import DeepSeekClient
from testgen_agent.models import ModelInfo

from .config import WorkflowConfig
from .service import DATA_ROOT, DEMO_FIXTURES, fixture_client, run_workflow

STATIC_ROOT = Path(__file__).with_name("web_static")
MAX_REQUEST_BYTES = 512 * 1024
DOWNLOADS = {"tests": "final/test_subject.py", "source": "final/subject.py",
             "report": "report.md", "summary": "summary.json", "state": "state.json"}


def samples() -> dict:
    return {name: {"source": (DATA_ROOT / f"{name}.py").read_text(encoding="utf-8"),
                   "spec": (DATA_ROOT / f"{name}.md").read_text(encoding="utf-8")}
            for name in DEMO_FIXTURES}


def validate_payload(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("请求必须是 JSON 对象。")
    source, spec = value.get("source"), value.get("spec")
    if not isinstance(source, str) or not source.strip() or len(source.encode("utf-8")) > 128 * 1024:
        raise ValueError("请提供不超过 128 KiB 的 Python 源码。")
    if not isinstance(spec, str) or not spec.strip() or len(spec.encode("utf-8")) > 128 * 1024:
        raise ValueError("请提供非空、大小不超过 128 KiB 的行为规格。")
    mode = value.get("mode", "real")
    if mode not in ("real", "offline"):
        raise ValueError("运行模式无效。")
    config = WorkflowConfig(mode=mode, max_rounds=value.get("max_rounds", 3),
                            execution_timeout_seconds=value.get("timeout", 30))
    sample = value.get("sample")
    if mode == "offline":
        if not isinstance(sample, str) or sample not in DEMO_FIXTURES:
            raise ValueError("离线模式请选择内置样例。")
        expected = samples()[sample]
        if source.strip() != expected["source"].strip() or spec.strip() != expected["spec"].strip():
            raise ValueError("编辑源码或规格后请使用 DeepSeek 模式。")
    return {"source": source, "spec": spec, "sample": sample, "config": config}


@dataclass(frozen=True)
class WebJob:
    id: str
    output: Path
    status: str = "queued"
    error: str | None = None


class JobStore:
    def __init__(self, project_root: Path, env_file: Path | None = None):
        self.root = project_root.resolve()
        self.env_file = env_file.resolve() if env_file is not None else self.root / ".env"
        self._jobs: dict[str, WebJob] = {}
        self._lock = threading.Lock()
        self.busy = threading.Lock()

    def create(self) -> WebJob:
        identity = uuid4().hex
        job = WebJob(identity, self.root / "outputs" / "web" / identity)
        with self._lock:
            self._jobs[identity] = job
        return job

    def get(self, identity: str) -> WebJob | None:
        with self._lock:
            return self._jobs.get(identity)

    def update(self, identity: str, **changes):
        with self._lock:
            self._jobs[identity] = replace(self._jobs[identity], **changes)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def job_snapshot(job: WebJob) -> dict:
    result = {"id": job.id, "status": job.status, "error": job.error,
              "outputDir": str(job.output), "events": [], "messages": []}
    for name in ("state", "summary"):
        try:
            result[name] = json.loads(_read(job.output / f"{name}.json") or "null")
        except json.JSONDecodeError:
            result[name] = None
    for name in ("events", "messages"):
        for line in (_read(job.output / f"{name}.jsonl") or "").splitlines():
            try:
                result[name].append(json.loads(line))
            except json.JSONDecodeError:
                break  # An append may still be in progress during polling.
    result["testCode"] = _read(job.output / DOWNLOADS["tests"])
    result["reportMarkdown"] = _read(job.output / DOWNLOADS["report"])
    result["downloads"] = {name: f"/api/runs/{job.id}/files/{name}"
                           for name, relative in DOWNLOADS.items() if (job.output / relative).is_file()}
    return result


def _run_job(store: JobStore, identity: str, payload: dict):
    try:
        job = store.get(identity)
        inputs = store.root / "outputs" / "web-inputs" / identity
        inputs.mkdir(parents=True, exist_ok=False)
        source, spec = inputs / "subject.py", inputs / "specification.md"
        source.write_text(payload["source"], encoding="utf-8")
        spec.write_text(payload["spec"], encoding="utf-8")
        config = payload["config"]
        if config.mode == "offline":
            client = fixture_client(payload["sample"])
        else:
            model_config = load_config(env_file=store.env_file)
            config = config.updated(model=ModelInfo.from_config(model_config))
            client = DeepSeekClient(model_config)
        store.update(identity, status="running")
        state = run_workflow(source=source, specification=spec, output=job.output,
                             config=config, client=client, with_summary=True)
        store.update(identity, status="finished" if state.workflow_status == "finished" else "failed",
                     error=state.error_code)
    except Exception:
        # The HTTP boundary never echoes model/configuration exception values.
        store.update(identity, status="failed", error="运行失败，请检查输入、模型配置及任务目录中的状态。")
    finally:
        store.busy.release()


class WebHandler(BaseHTTPRequestHandler):
    server_version = "TestWorkflow/0.1"

    def log_message(self, format, *args):
        # Polling is frequent; retain submissions and HTTP errors only.
        if self.command == "POST" or (len(args) > 1 and str(args[1]).startswith(("4", "5"))):
            super().log_message(format, *args)

    def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, status=HTTPStatus.OK):
        self._send(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def _local_request(self) -> bool:
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed:
            self._json({"error": "仅允许本机访问。"}, HTTPStatus.FORBIDDEN)
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            self._json({"error": "不接受跨站请求。"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def do_GET(self):
        if not self._local_request():
            return
        path = urlsplit(self.path).path
        static = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                  "/styles.css": ("styles.css", "text/css")}
        if path in static:
            name, mime = static[path]
            self._send((STATIC_ROOT / name).read_bytes(), mime + "; charset=utf-8")
            return
        store = self.server.store
        if path == "/api/bootstrap":
            try:
                config = load_config(env_file=store.env_file)
                settings = {"ready": True, "model": config.model, "max_rounds": config.max_rounds,
                            "timeout": config.execution_timeout_seconds}
            except ValueError:
                settings = {"ready": False, "model": None, "max_rounds": 3, "timeout": 30}
            self._json({"config": settings, "samples": samples()})
            return
        parts = path.strip("/").split("/")
        if len(parts) in (3, 5) and parts[:2] == ["api", "runs"]:
            job = store.get(parts[2])
            if job is not None:
                if len(parts) == 3:
                    self._json(job_snapshot(job))
                    return
                if parts[3] == "files" and parts[4] in DOWNLOADS:
                    file = job.output / DOWNLOADS[parts[4]]
                    if file.is_file():
                        self._send(file.read_bytes(), "application/octet-stream", filename=file.name)
                        return
        self._json({"error": "未找到任务或文件。"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self._local_request():
            return
        if urlsplit(self.path).path != "/api/runs":
            self._json({"error": "未找到接口。"}, HTTPStatus.NOT_FOUND)
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._json({"error": "请发送 application/json。"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._json({"error": "请求大小无效。"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            payload = validate_payload(json.loads(self.rfile.read(length).decode("utf-8")))
        except (UnicodeError, ValueError):
            self._json({"error": "输入无效；检查源码、规格、轮次、超时和离线样例是否匹配。"}, HTTPStatus.BAD_REQUEST)
            return
        store = self.server.store
        if not store.busy.acquire(blocking=False):
            self._json({"error": "已有任务运行中，请等待完成。"}, HTTPStatus.CONFLICT)
            return
        job = store.create()
        try:
            threading.Thread(target=_run_job, args=(store, job.id, payload), daemon=True).start()
        except Exception:
            store.busy.release()
            store.update(job.id, status="failed", error="无法启动任务。")
            self._json({"error": "无法启动任务。"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._json({"id": job.id, "status": "queued"}, HTTPStatus.ACCEPTED)


class WebServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], project_root: Path, env_file: Path | None = None):
        if address[0] not in ("127.0.0.1", "localhost"):
            raise ValueError("the workbench must bind to loopback")
        super().__init__(address, WebHandler)
        self.store = JobStore(project_root, env_file)


def serve(host: str, port: int, project_root: Path, env_file: Path | None = None) -> int:
    with WebServer((host, port), project_root, env_file) as server:
        print(f"Workflow Web：http://{host}:{server.server_port}", flush=True)
        print("按 Ctrl+C 停止。DeepSeek 模式读取本地配置；离线模式不调用模型。", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    from .cli import main as cli_main
    import sys
    return cli_main(["web", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
