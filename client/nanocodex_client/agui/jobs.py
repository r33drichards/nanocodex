"""Background jobs for the AG-UI bridge: **crons** and **monitors**.

A job runs on a schedule (5-field crontab or a plain interval) and delivers
into codex. Three orthogonal choices define a job:

- action — `prompt` (a text delivered as-is) or `code` (JavaScript executed
  on a shared mcp-v8 over streamable HTTP; the run's output is what gets
  delivered). Each job's code runs under its own stable mcp-v8 session id
  (`job-<id>`), so a job can keep cross-firing state in the sandbox when the
  shared server persists heaps.
- kind — a `cron` delivers on every firing; a `monitor` is the conditional
  variant: it must have `code`, and delivers only when the check triggers
  (`fire_on="truthy"`: output non-empty and not false/null/undefined/0;
  `fire_on="change"`: output differs from the previous run — the first run
  only primes).
- target — `thread_id` set: deliver into that codex thread, STEERING the
  in-flight turn if one is running, else starting a new turn on it;
  `thread_id` unset: isolated — every delivery starts a brand-new bridge
  thread (deployment sandbox preset, ui + jobs servers, instructions).

The store is a ThreadStore-style JSON file (`AGUI_JOBS_PATH`; in-memory when
unset). The Scheduler is one asyncio task started by the bridge app; firings
run as their own tasks so a slow turn never blocks the tick loop.
"""

from __future__ import annotations

import asyncio
import os
import time as _time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from ..core import Nanocodex, RpcError
from . import cronexpr

KINDS = ("cron", "monitor")
FIRE_ONS = ("always", "truthy", "change")
MIN_EVERY_SECS = 5.0
HISTORY_LIMIT = 20
OUTPUT_LIMIT = 4000  # chars of job output kept in run records

JOBS_MCP_URL_ENV = "NANOCODEX_JOBS_MCP_URL"
RUN_JS_TOOL_ENV = "NANOCODEX_RUN_JS_TOOL"

# What a firing injects. `{prompt}` / `{output}` / `{name}` / `{kind}` /
# `{at}` are replaced (plain replace, not str.format — job code/output may
# contain braces).
DEFAULT_PROMPT_DELIVERY = '[scheduled job "{name}" ({kind}) fired at {at}] {prompt}'
DEFAULT_CODE_DELIVERY = '[scheduled job "{name}" ({kind}) fired at {at}] output:\n{output}'


class JobError(ValueError):
    """Invalid job definition or operation (maps to HTTP 400 / MCP tool error)."""


@dataclass
class Job:
    id: str
    name: str = ""
    kind: str = "cron"              # cron | monitor
    schedule: str | None = None     # 5-field crontab / @alias (xor `every`)
    every: float | None = None      # plain interval in seconds (xor `schedule`)
    prompt: str | None = None       # action: text (xor `code`)
    code: str | None = None         # action: mcp-js JavaScript (xor `prompt`)
    thread_id: str | None = None    # codex thread target; None = isolated
    fire_on: str = "always"         # always | truthy | change
    deliver_prompt: str | None = None  # custom delivery template
    enabled: bool = True
    timeout: float = 600.0
    owner_thread_id: str | None = None  # codex thread that created the job
    created_at: str = ""
    last_output: str | None = None  # monitor change-detection memory
    last: dict | None = None        # most recent run record
    history: list = field(default_factory=list)  # run records, newest first


_JOB_FIELDS = {f.name for f in fields(Job)}
# Caller-settable fields (everything else is scheduler bookkeeping).
MUTABLE_FIELDS = ("name", "kind", "schedule", "every", "prompt", "code",
                  "thread_id", "fire_on", "deliver_prompt", "enabled", "timeout")


def _validate(job: Job) -> None:
    if job.kind not in KINDS:
        raise JobError(f"kind must be one of {KINDS}, got {job.kind!r}")
    if (job.schedule is None) == (job.every is None):
        raise JobError("give exactly one of `schedule` (crontab) or `every` (seconds)")
    if job.schedule is not None:
        try:
            cronexpr.parse(job.schedule)
        except ValueError as e:
            raise JobError(f"bad schedule: {e}")
    if job.every is not None:
        try:
            job.every = float(job.every)
        except (TypeError, ValueError):
            raise JobError(f"`every` must be seconds, got {job.every!r}")
        if job.every < MIN_EVERY_SECS:
            raise JobError(f"`every` must be >= {MIN_EVERY_SECS} seconds")
    if (job.prompt is None) == (job.code is None):
        raise JobError("give exactly one of `prompt` or `code`")
    if job.kind == "monitor":
        if job.code is None:
            raise JobError("a monitor's check must be `code` (mcp-js JavaScript)")
        if job.fire_on == "always":
            job.fire_on = "truthy"
        if job.fire_on not in ("truthy", "change"):
            raise JobError("monitor fire_on must be 'truthy' or 'change'")
    else:
        if job.fire_on != "always":
            raise JobError("a cron always fires — use kind='monitor' for fire_on gating")
    if job.timeout is not None:
        job.timeout = float(job.timeout)


def _truthy(output: str | None) -> bool:
    return (output or "").strip().lower() not in ("", "false", "null", "undefined", "0")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JobStore:
    """Persisted job registry: a single flat JSON file rewritten atomically
    (same trade-offs as ThreadStore — plenty for a single-bridge deployment).
    No path = in-memory."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.jobs: dict[str, Job] = {}
        self._path = Path(path) if path else None
        if self._path is not None:
            self._load()

    def add(self, job: Job) -> Job:
        self.jobs[job.id] = job
        self.save()
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        found = self.jobs.pop(job_id, None) is not None
        if found:
            self.save()
        return found

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created_at)

    def _load(self) -> None:
        """Best-effort: a missing or corrupt file starts empty, never a bridge
        that won't boot."""
        import json

        try:
            raw = json.loads(self._path.read_text())
            for jid, d in raw.items():
                if isinstance(d, dict) and "id" in d:
                    self.jobs[jid] = Job(**{k: v for k, v in d.items() if k in _JOB_FIELDS})
        except FileNotFoundError:
            pass
        except Exception as err:
            print(f"[jobs] ignoring unreadable jobs file {self._path}: {err}")

    def save(self) -> None:
        if self._path is None:
            return
        import json

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps({jid: asdict(j) for jid, j in self.jobs.items()}, indent=1))
            os.replace(tmp, self._path)
        except Exception as err:
            print(f"[jobs] failed to persist jobs to {self._path}: {err}")


def _jobs_mcp_url() -> str:
    """Where job `code` runs: a shared mcp-v8 speaking streamable HTTP. The
    standalone images run one on :8080; remote deployments reuse the same URL
    the `remote` sandbox preset points threads at."""
    return (os.environ.get(JOBS_MCP_URL_ENV)
            or os.environ.get("NANOCODEX_MCP_V8_URL")
            or "http://127.0.0.1:8080/mcp")


async def run_js(code: str, session_key: str, timeout: float = 180.0) -> str:
    """Run mcp-js code on the shared mcp-v8 over streamable HTTP, session-keyed
    so each job keeps its own persistent sandbox state (when the server
    persists heaps). Returns the run's text output."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(
        url=_jobs_mcp_url(), headers={"X-MCP-Session-Id": session_key})
    tool = os.environ.get(RUN_JS_TOOL_ENV, "run_js")
    async with asyncio.timeout(timeout):
        async with Client(transport) as client:
            res = await client.call_tool(tool, {"code": code}, raise_on_error=False)
    blocks = getattr(res, "content", None) or (res if isinstance(res, list) else [])
    text = "\n".join(t for t in (getattr(b, "text", None) for b in blocks) if t)
    if getattr(res, "is_error", False) or getattr(res, "isError", False):
        raise RuntimeError(f"run_js failed: {text[:500] or 'no output'}")
    return text


class Scheduler:
    """Owns the job lifecycle: CRUD (validating + [re]arming), the tick loop,
    and firing (check -> gate -> deliver -> record). `connect`/`js_runner` are
    injectable for tests."""

    TICK_SECS = 1.0

    def __init__(self, store: JobStore, connect=None, js_runner=None):
        self.store = store
        self._connect = connect or Nanocodex.connect
        self._js = js_runner or run_js
        self._next: dict[str, float] = {}   # job id -> unix due time
        self._running: set[str] = set()     # job ids with a firing in flight
        self._tasks: set[asyncio.Task] = set()
        for job in store.list():
            try:
                self._arm(job)
            except Exception as err:  # a bad persisted job must not stop the bridge
                print(f"[jobs] not arming job {job.id} ({job.name!r}): {err}")

    # ── CRUD (shared by the REST endpoints and the MCP tools) ─────────────
    def create(self, data: dict, owner_thread_id: str | None = None) -> Job:
        data = dict(data)
        if "every_seconds" in data:  # MCP tool arg alias
            data.setdefault("every", data.pop("every_seconds"))
        unknown = set(data) - set(MUTABLE_FIELDS)
        if unknown:
            raise JobError(f"unknown job fields: {sorted(unknown)}")
        job = Job(id=uuid.uuid4().hex[:12], created_at=_now_iso(),
                  owner_thread_id=owner_thread_id,
                  **{k: v for k, v in data.items() if v is not None})
        _validate(job)
        self.store.add(job)
        self._arm(job)
        return job

    def update(self, job_id: str, patch: dict) -> Job:
        job = self._require(job_id)
        patch = dict(patch)
        if "every_seconds" in patch:
            patch.setdefault("every", patch.pop("every_seconds"))
        unknown = set(patch) - set(MUTABLE_FIELDS)
        if unknown:
            raise JobError(f"unknown job fields: {sorted(unknown)}")
        # Switching between schedule and every: setting one clears the other.
        if patch.get("schedule") is not None and "every" not in patch:
            patch["every"] = None
        if patch.get("every") is not None and "schedule" not in patch:
            patch["schedule"] = None
        for k, v in patch.items():
            setattr(job, k, v)
        _validate(job)
        self.store.save()
        self._arm(job)
        return job

    def delete(self, job_id: str) -> bool:
        self._next.pop(job_id, None)
        return self.store.delete(job_id)

    def run_now(self, job_id: str) -> Job:
        """Fire immediately (even if disabled); the firing runs as a task."""
        job = self._require(job_id)
        self._spawn(job)
        return job

    def describe(self, job: Job) -> dict:
        d = asdict(job)
        due = self._next.get(job.id)
        d["next_run"] = datetime.fromtimestamp(due).isoformat(timespec="seconds") if due else None
        return d

    def _require(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise JobError(f"unknown job {job_id!r}")
        return job

    # ── scheduling ────────────────────────────────────────────────────────
    def _arm(self, job: Job) -> None:
        if not job.enabled:
            self._next.pop(job.id, None)
            return
        if job.every is not None:
            self._next[job.id] = _time.time() + job.every
        else:
            self._next[job.id] = cronexpr.parse(job.schedule).next_after(datetime.now()).timestamp()

    async def run(self) -> None:
        """The bridge's scheduler task: tick forever."""
        while True:
            try:
                self.tick_due()
            except Exception as err:  # a bad tick must never kill the loop
                print(f"[jobs] tick failed: {err}")
            await asyncio.sleep(self.TICK_SECS)

    def tick_due(self, now: float | None = None) -> None:
        now = _time.time() if now is None else now
        for job_id, due in list(self._next.items()):
            if due > now:
                continue
            job = self.store.get(job_id)
            if job is None:
                self._next.pop(job_id, None)
                continue
            self._arm(job)  # schedule the next firing before running this one
            self._spawn(job)

    def _spawn(self, job: Job) -> None:
        task = asyncio.create_task(self.fire(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── firing ────────────────────────────────────────────────────────────
    async def fire(self, job: Job) -> dict:
        """One firing: run the check (if `code`), gate (if monitor), deliver,
        record. Never raises — failures land in the job's run history."""
        if job.id in self._running:
            return self._record(job, "skipped", "previous run still in progress")
        self._running.add(job.id)
        try:
            output = None
            if job.code is not None:
                output = await self._js(job.code, session_key=f"job-{job.id}",
                                        timeout=min(job.timeout, 300.0))
            if job.kind == "monitor":
                fired = self._monitor_fires(job, output)
                job.last_output = output
                if not fired:
                    return self._record(job, "checked", f"no fire ({job.fire_on})", output=output)
            detail = await self._deliver(job, self._delivery_text(job, output))
            return self._record(job, "fired", detail, output=output)
        except Exception as err:
            return self._record(job, "error", f"{type(err).__name__}: {err}")
        finally:
            self._running.discard(job.id)

    def _monitor_fires(self, job: Job, output: str | None) -> bool:
        if job.fire_on == "change":
            # First run primes the memory without firing.
            return job.last_output is not None and output != job.last_output
        return _truthy(output)

    def _delivery_text(self, job: Job, output: str | None) -> str:
        template = job.deliver_prompt or (
            DEFAULT_PROMPT_DELIVERY if job.prompt is not None else DEFAULT_CODE_DELIVERY)
        # Plain replace, not str.format: output/prompt routinely contain braces.
        for key, value in (("{name}", job.name or job.id), ("{kind}", job.kind),
                           ("{at}", _now_iso()), ("{prompt}", job.prompt or ""),
                           ("{output}", output or "")):
            template = template.replace(key, value)
        return template

    async def _deliver(self, job: Job, text: str) -> dict:
        """Point 3 of the design: a session target steers the current agent
        when a turn is in flight (else starts a turn on that thread); no
        target = the output starts a new isolated session."""
        import nanocodex_client.agui.router as R  # late: router must not import jobs

        nc = await self._connect()
        try:
            if job.thread_id:
                tid = job.thread_id
                if tid in R._active:  # a bridge-run turn is in flight -> steer it
                    await nc.steer_turn(tid, text)
                    return {"mode": "steer", "threadId": tid}
                await nc.resume_thread(tid)
                R._active.add(tid)
                try:
                    result = await nc.run_turn(tid, text, timeout=job.timeout)
                except RpcError:
                    # another frontend holds the in-flight turn -> steer instead
                    await nc.steer_turn(tid, text)
                    return {"mode": "steer", "threadId": tid}
                finally:
                    R._active.discard(tid)
                return {"mode": "turn", "threadId": tid, **self._turn_summary(result)}
            # Isolated: a fresh bridge-wired thread per firing.
            pseudo = f"job-{job.id}-{uuid.uuid4().hex[:8]}"
            tid = await R.create_bridge_thread(nc, pseudo, approvals=False)
            result = await nc.run_turn(tid, text, timeout=job.timeout)
            return {"mode": "isolated", "threadId": tid, **self._turn_summary(result)}
        finally:
            await nc.close()

    @staticmethod
    def _turn_summary(result: dict) -> dict:
        replies = result.get("agent_messages") or []
        return {"status": (result.get("turn") or {}).get("status"),
                "reply": (replies[-1] if replies else "")[:500]}

    def _record(self, job: Job, status: str, detail, output: str | None = None) -> dict:
        rec = {"at": _now_iso(), "status": status, "detail": detail}
        if output is not None:
            rec["output"] = output[:OUTPUT_LIMIT]
        job.last = rec
        job.history = [rec] + (job.history or [])[: HISTORY_LIMIT - 1]
        self.store.save()
        return rec
