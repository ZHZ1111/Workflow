"""S9 bounded real-model acceptance. Default: prepare only, no model requests.

--execute runs exactly three repository samples through the shared production
service. Audit records contain request hashes/counts, never headers or API keys.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from testgen_agent.config import load_config
from testgen_agent.llm_client import DeepSeekClient, RetryPolicy
from test_workflow.config import WorkflowConfig
from test_workflow.quality_client import DeepSeekReviewClient, ReviewRetryPolicy
from test_workflow.reporting import inspect_summary
from test_workflow.service import run_workflow
from test_workflow.storage import atomic_write, file_digest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = (("calculation", 2), ("quality_boundary", 3), ("discount", 2))


def save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class DispatchBudget:
    def __init__(self, directory, *, maximum=18, per_case=6):
        self.directory = directory
        self.maximum, self.per_case = maximum, per_case
        self.requests = []
        self.blocked = 0

    def reserve(self, sample, role, request):
        if (request.url.scheme != "https" or request.url.host != "api.deepseek.com"
                or request.url.port not in (None, 443) or request.method != "POST"
                or request.url.path not in ("/chat/completions", "/v1/chat/completions")):
            self.blocked += 1
            raise httpx.RequestError("acceptance endpoint rejected", request=request)
        if (len(self.requests) >= self.maximum or sum(row["sample"] == sample for row in self.requests) >= self.per_case
                or len(request.content) > 131072):
            self.blocked += 1
            raise httpx.RequestError("acceptance request budget exhausted", request=request)
        payload = json.loads(request.content)
        if type(payload.get("max_tokens")) is not int or not 1 <= payload["max_tokens"] <= 8192:
            self.blocked += 1
            raise httpx.RequestError("acceptance output budget rejected", request=request)
        record = {"number": len(self.requests) + 1, "sample": sample, "role": role,
                  "body_bytes": len(request.content), "body_sha256": hashlib.sha256(request.content).hexdigest(),
                  "status_code": None, "started_at": datetime.now(timezone.utc).isoformat()}
        self.requests.append(record)
        self.persist()
        return record

    def persist(self):
        save(self.directory / "dispatches.json", {"dispatch_attempts": len(self.requests), "maximum": self.maximum,
             "per_case_maximum": self.per_case, "blocked_before_dispatch": self.blocked, "requests": self.requests})


class AuditedTransport(httpx.BaseTransport):
    def __init__(self, budget, sample, role, proxy=None):
        self.budget, self.sample, self.role, self.proxy = budget, sample, role, proxy

    def handle_request(self, request):
        record = self.budget.reserve(self.sample, self.role, request)
        started = perf_counter()
        try:
            # One pool per attempt lets the same transport survive SDK context closures.
            with httpx.HTTPTransport(retries=0, proxy=self.proxy) as transport:
                response = transport.handle_request(request)
                record["status_code"] = response.status_code
                response.read()
                return response
        except Exception as error:
            record["error_type"] = type(error).__name__
            raise
        finally:
            record["duration_seconds"] = round(perf_counter() - started, 3)
            self.budget.persist()


def manifest(config):
    parsed = urlsplit(config.base_url)
    if parsed.scheme != "https" or parsed.hostname != "api.deepseek.com" or parsed.port not in (None, 443):
        raise ValueError("S9 acceptance requires the configured official HTTPS DeepSeek endpoint")
    return {"prepared_at": datetime.now(timezone.utc).isoformat(), "model": config.model, "base_url": config.base_url,
        "request_timeout_seconds": config.request_timeout_seconds, "max_output_tokens_per_request": config.max_tokens,
        "http_dispatch_limit": 18, "http_per_case_limit": 6, "maximum_request_body_bytes": 131072,
        "generation_attempts_per_call": 2, "review_attempts_per_call": 2,
        "transient_retries": 0, "format_repairs_per_call": 1,
        "external_content": "Selected source/specification, generated plans/tests, execution/coverage excerpts, feedback and task metadata. Key only in authentication; never recorded.",
        "samples": [{"name": name, "max_rounds": rounds,
            "source": f"examples/{name}.py", "source_sha256": file_digest(ROOT / f"examples/{name}.py"),
            "spec": f"examples/{name}.md", "spec_sha256": file_digest(ROOT / f"examples/{name}.md")}
            for name, rounds in SAMPLES]}


def verify_sample(sample, summary):
    if summary["workflow_status"] != "finished":
        return False
    if sample == "discount":
        return summary["task_status"] == "needs_review" and summary["stop_reason"] == "suspected_source_defect" and summary["selected_all_passed"] is False
    return summary["task_status"] == "completed" and summary["exported_quality_approved"] is True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Send the declared samples to DeepSeek, subject to the saved budget")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/quality-s9" / uuid4().hex)
    args = parser.parse_args()
    directory = args.output.resolve()
    if not directory.is_relative_to(ROOT / "outputs") or directory.exists():
        parser.error("output must be a new directory inside this workspace's outputs")
    config = load_config(env_file=args.env_file, overrides={"max_tokens": 8192, "request_timeout_seconds": 60})
    prepared = manifest(config)
    directory.mkdir(parents=True)
    save(directory / "manifest.json", prepared)
    print(f"Evidence: {directory}", flush=True)
    if not args.execute:
        print("Prepared three samples; no model requests sent. Use --execute for the saved scope.", flush=True)
        return 0
    budget = DispatchBudget(directory)
    results = {"mode": "real", "manifest": "manifest.json", "cases": [], "errors": [], "success": False}
    save(directory / "results.json", results)
    for sample, rounds in SAMPLES:
        print(f"S9 real: {sample}...", flush=True)
        try:
            source, spec = ROOT / f"examples/{sample}.py", ROOT / f"examples/{sample}.md"
            entry = next(item for item in prepared["samples"] if item["name"] == sample)
            if file_digest(source) != entry["source_sha256"] or file_digest(spec) != entry["spec_sha256"]:
                raise ValueError("prepared sample changed")
            generation = DeepSeekClient(config, retry_policy=RetryPolicy(max_transient_retries=0, max_format_repairs=1),
                transport=AuditedTransport(budget, sample, "generation", config.proxy_url))
            review = DeepSeekReviewClient(config, retry_policy=ReviewRetryPolicy(max_http_attempts=2, max_transient_retries=0, max_format_repairs=1),
                transport=AuditedTransport(budget, sample, "review", config.proxy_url))
            state = run_workflow(source=source, specification=spec, output=directory / sample,
                config=WorkflowConfig(mode="real", quality_review_enabled=True, max_rounds=rounds),
                client=generation, review_client=review, with_summary=True)
            summary = inspect_summary(state)
            assert file_digest(source) == entry["source_sha256"] and file_digest(spec) == entry["spec_sha256"]
            feedback = [item for item in summary["messages"] if item["type"] == "quality_feedback"]
            results["cases"].append({"sample": sample, "passed": verify_sample(sample, summary),
                "output": str(state.run_dir.relative_to(ROOT)), "quality_feedback_count": len(feedback),
                "consumed_quality_feedback": sum(item["consumed"] for item in feedback),
                **{key: summary[key] for key in ("workflow_status", "task_status", "stop_reason", "error_code", "quality_status",
                    "quality_stop_reason", "rounds", "selected_round", "exported_quality_approved", "generation_calls", "review_calls",
                    "external_requests", "model_usage", "pytest_processes", "selected_execution", "selected_coverage", "final_test_sha256")}})
            print(f"  {state.task_status}/{state.quality_status}; rounds={summary['rounds']}; requests={summary['external_requests']}; quality feedback={len(feedback)}", flush=True)
        except Exception as error:
            results["errors"].append({"sample": sample, "error_type": type(error).__name__})
            print(f"  failed ({type(error).__name__}); no automatic replay", flush=True)
        finally:
            budget.persist()
            results["dispatch_attempts"] = len(budget.requests)
            results["success"] = len(results["cases"]) == len(SAMPLES) and all(item["passed"] for item in results["cases"]) and not results["errors"]
            save(directory / "results.json", results)
    return 0 if results["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
