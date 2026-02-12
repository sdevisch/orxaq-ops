"""Rate-limited RPA scheduler with concurrency caps and retry backoff."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class SchedulerPolicy:
    max_concurrent_browsers: int = 1
    per_domain_interval_sec: float = 0.0
    failure_backoff_base_sec: float = 1.0
    failure_backoff_max_sec: float = 60.0
    max_retries: int = 1


@dataclass(frozen=True)
class RPAJob:
    id: str
    domain: str
    command: list[str]
    max_retries: int | None = None
    timeout_sec: int = 300


@dataclass(frozen=True)
class AttemptRecord:
    job_id: str
    domain: str
    attempt: int
    status: str
    started_at: str
    ended_at: str
    duration_ms: float
    error: str
    backoff_sec: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_payload(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("scheduler config must be a JSON object")
    return payload


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_domain(value: str) -> str:
    return value.strip().lower()


def _build_orxaq_command(
    *,
    python_exe: str,
    orxaq_cli_path: Path,
    url: str,
    domain: str,
    evidence_root: str,
    run_id: str,
    task_id: str,
    allow_http: bool,
    allow_private_network: bool,
    explicit_allow: bool,
) -> list[str]:
    cmd = [
        python_exe,
        str(orxaq_cli_path),
        "rpa-screenshot",
        url,
        "--allow-domain",
        domain,
        "--evidence-root",
        evidence_root,
        "--run-id",
        run_id,
        "--task-id",
        task_id,
    ]
    if allow_http:
        cmd.append("--allow-http")
    if allow_private_network:
        cmd.append("--allow-private-network")
    if explicit_allow:
        cmd.append("--explicit-allow")
    return cmd


def load_scheduler_config(root: str, config_path: str) -> tuple[SchedulerPolicy, list[RPAJob]]:
    root_path = Path(root).expanduser().resolve()
    config_file = Path(config_path).expanduser()
    if not config_file.is_absolute():
        config_file = (root_path / config_file).resolve()
    payload = _load_payload(config_file)

    policy_raw = payload.get("policy", {})
    if not isinstance(policy_raw, dict):
        policy_raw = {}
    policy = SchedulerPolicy(
        max_concurrent_browsers=max(1, _safe_int(policy_raw.get("max_concurrent_browsers", 1), 1)),
        per_domain_interval_sec=max(0.0, _safe_float(policy_raw.get("per_domain_interval_sec", 0.0), 0.0)),
        failure_backoff_base_sec=max(0.0, _safe_float(policy_raw.get("failure_backoff_base_sec", 1.0), 1.0)),
        failure_backoff_max_sec=max(0.0, _safe_float(policy_raw.get("failure_backoff_max_sec", 60.0), 60.0)),
        max_retries=max(0, _safe_int(policy_raw.get("max_retries", 1), 1)),
    )

    run_id = str(payload.get("run_id", _now_iso().replace(":", "").replace("-", ""))).strip()
    python_exe = str(payload.get("python_executable", "python3")).strip() or "python3"
    evidence_root = str(payload.get("evidence_root", "./artifacts/rpa_evidence")).strip() or "./artifacts/rpa_evidence"
    orxaq_cli_path = Path(str(payload.get("orxaq_cli_path", "../orxaq/orxaq_cli.py"))).expanduser()
    if not orxaq_cli_path.is_absolute():
        orxaq_cli_path = (root_path / orxaq_cli_path).resolve()

    jobs_raw = payload.get("jobs", [])
    if not isinstance(jobs_raw, list):
        raise ValueError("scheduler config must define jobs as a list")
    jobs: list[RPAJob] = []
    for item in jobs_raw:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id", "")).strip()
        domain = _normalize_domain(str(item.get("domain", "")))
        if not job_id or not domain:
            continue
        command_raw = item.get("command")
        if isinstance(command_raw, list) and all(isinstance(part, str) for part in command_raw):
            command = [str(part) for part in command_raw]
        else:
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            command = _build_orxaq_command(
                python_exe=python_exe,
                orxaq_cli_path=orxaq_cli_path,
                url=url,
                domain=domain,
                evidence_root=evidence_root,
                run_id=run_id,
                task_id=job_id,
                allow_http=bool(item.get("allow_http", False)),
                allow_private_network=bool(item.get("allow_private_network", False)),
                explicit_allow=bool(item.get("explicit_allow", False)),
            )
        jobs.append(
            RPAJob(
                id=job_id,
                domain=domain,
                command=command,
                max_retries=_safe_int(item.get("max_retries"), -1) if item.get("max_retries") is not None else None,
                timeout_sec=max(1, _safe_int(item.get("timeout_sec", 300), 300)),
            )
        )
    return policy, jobs


def _default_run_job(job: RPAJob) -> tuple[bool, str]:
    env = os.environ.copy()
    env["CI"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        job.command,
        text=True,
        capture_output=True,
        timeout=max(1, int(job.timeout_sec)),
        check=False,
        env=env,
    )
    if result.returncode == 0:
        return True, ""
    combined = f"{result.stdout}\n{result.stderr}".strip()
    return False, combined[:2000]


def run_rpa_schedule(
    *,
    policy: SchedulerPolicy,
    jobs: list[RPAJob],
    run_job: Callable[[RPAJob], tuple[bool, str]] | None = None,
    now_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Execute RPA jobs with concurrency cap, domain rate limits, and retry backoff."""
    if not jobs:
        return {
            "ok": True,
            "jobs_total": 0,
            "jobs_succeeded": 0,
            "jobs_failed": 0,
            "max_concurrency_seen": 0,
            "attempts": [],
        }

    runner = run_job or _default_run_job
    domain_lock = threading.Lock()
    next_domain_slot: dict[str, float] = {}
    semaphore = threading.Semaphore(max(1, policy.max_concurrent_browsers))
    active_lock = threading.Lock()
    active_count = 0
    max_seen = 0
    records: list[AttemptRecord] = []
    records_lock = threading.Lock()
    success_by_job: dict[str, bool] = {}
    attempts_by_job: dict[str, int] = {job.id: 0 for job in jobs}

    def reserve_domain_slot(domain: str) -> None:
        while True:
            wait_for = 0.0
            with domain_lock:
                now = now_fn()
                ready_at = next_domain_slot.get(domain, 0.0)
                if now >= ready_at:
                    next_domain_slot[domain] = now + max(0.0, policy.per_domain_interval_sec)
                    return
                wait_for = max(0.0, ready_at - now)
            if wait_for > 0:
                sleep_fn(wait_for)

    def execute_job(job: RPAJob) -> None:
        nonlocal active_count, max_seen
        max_retries = policy.max_retries if job.max_retries is None else max(0, int(job.max_retries))
        backoff = max(0.0, policy.failure_backoff_base_sec)
        for attempt in range(1, max_retries + 2):
            reserve_domain_slot(job.domain)
            semaphore.acquire()
            started_monotonic = now_fn()
            started_at = _now_iso()
            with active_lock:
                active_count += 1
                if active_count > max_seen:
                    max_seen = active_count
            try:
                ok, error = runner(job)
            finally:
                ended_monotonic = now_fn()
                ended_at = _now_iso()
                with active_lock:
                    active_count = max(0, active_count - 1)
                semaphore.release()

            attempts_by_job[job.id] += 1
            record = AttemptRecord(
                job_id=job.id,
                domain=job.domain,
                attempt=attempt,
                status="success" if ok else "failed",
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=round(max(0.0, ended_monotonic - started_monotonic) * 1000.0, 3),
                error="" if ok else error,
                backoff_sec=0.0 if ok else backoff,
            )
            with records_lock:
                records.append(record)
            if ok:
                success_by_job[job.id] = True
                return
            if attempt > max_retries:
                success_by_job[job.id] = False
                return
            if backoff > 0:
                sleep_fn(backoff)
                backoff = min(max(backoff * 2.0, backoff), max(0.0, policy.failure_backoff_max_sec))

    threads = [threading.Thread(target=execute_job, args=(job,), daemon=True) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    jobs_total = len(jobs)
    jobs_succeeded = sum(1 for value in success_by_job.values() if value)
    jobs_failed = jobs_total - jobs_succeeded
    return {
        "ok": jobs_failed == 0,
        "jobs_total": jobs_total,
        "jobs_succeeded": jobs_succeeded,
        "jobs_failed": jobs_failed,
        "max_concurrency_seen": max_seen,
        "attempts_total": sum(attempts_by_job.values()),
        "policy": asdict(policy),
        "attempts": [item.to_dict() for item in records],
    }


def run_rpa_schedule_from_config(
    *,
    root: str = ".",
    config_path: str = "./config/rpa_schedule.example.json",
    output_path: str = "./artifacts/autonomy/rpa_scheduler_report.json",
) -> dict[str, Any]:
    policy, jobs = load_scheduler_config(root=root, config_path=config_path)
    report = run_rpa_schedule(policy=policy, jobs=jobs)
    root_path = Path(root).expanduser().resolve()
    output_file = Path(output_path).expanduser()
    if not output_file.is_absolute():
        output_file = (root_path / output_file).resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["output_path"] = str(output_file)
    return report
