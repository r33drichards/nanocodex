"""Webhook-driven CraftOS turtle-program solver — a validation "stop hook".

This is the AG-UI bridge realization of the control flow in
https://robertwestenberg.com "Simple Control Flow for Automatically Steering
Agents": a deterministic validator sits *inside* the agent loop so the agent
keeps working until the environment's actual state passes a check, not until
the model merely claims success.

    while budget remains:
        run one codex turn (model writes /work/turtle.lua)
        # the "stop hook": validate the ACTUAL file against the sim
        pass, output = run craftos(sim, program=turtle.lua)
        if pass: break            # SIM_RESULT: PASS
        feedback = the assertion failures  # fed into the next turn

Codex has two native primitives we lean on rather than reinvent:

- **The goal API** (`thread/goal/set`) gives us the *budget* half natively: an
  objective plus a `tokenBudget`, with codex tracking `tokensUsed` /
  `timeUsedSeconds` for us. We use it (best-effort — feature-detected so it
  degrades on forks without it) to carry the token budget and to mark the goal
  terminal when we finish.
- Codex also ships **native `[features].hooks` Stop hooks** (see
  https://developers.openai.com/codex/hooks#stop) that run a *command* when a
  turn stops. They are the right tool when the validator is a shell command in
  a normal codex workspace. This deployment is the opposite: the model's only
  tool is a locked-down `run_js` sandbox (no shell, `sandbox_mode=read-only`),
  and the validator has to run the CraftOS **wasm** engine and then publish an
  S3 result a webhook caller polls — orchestration a per-turn command hook
  can't do. So we realize the same Stop-hook *control flow* in the bridge,
  which is the thing "sitting on top of codex" already. The goal API supplies
  the budget; the bridge loop supplies the validation gate.

## The flow

1. A caller `POST`s a CraftOS sim spec (nodes + a `world_lua` `test(sim)`
   post-condition) with the turtle node's `program` left blank, plus an
   optional budget. See `SolveRequest`.
2. We immediately return a presigned S3 GET URL (the "poll_url") and a job id.
   The caller polls that URL; the object doesn't exist until the job finishes.
3. In the background we drive codex to write the missing turtle program to a
   canonical path (`/work/turtle.lua`) and validate it against the sim each
   turn until `SIM_RESULT: PASS` or the budget runs out.
4. We upload `{status, program|error, sim_output, turns, tokens}` to the S3
   object. The poll now succeeds.

## Independence & the shared fs label

The validator must read the *actual* file the model wrote — not trust the
model's word, and not read a host-disk passthrough (which doesn't hold in the
shared/remote mcp-v8 topology). Instead the codex thread's sandbox and the
validator both address the SAME mcp-v8 keyed by one per-job **fs label**
(`X-MCP-Session-Id`, the mechanism the `remote` sandbox preset already uses).
The model's `run_js` writes `/work/turtle.lua` under that label; the
validator's `run_js`, under the same label, reads it back and runs the sim —
same `/work`, and every job gets its own label so jobs never see each other.
The validator's sim spec (world + `test`) is bridge-controlled, so the model
can only supply the program text; it cannot change what "pass" means.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..core import Nanocodex, RpcError, SandboxSpec
from .sandbox import LANGUAGES_INSTRUCTIONS as _LANGUAGES_INSTRUCTIONS

# ── config ────────────────────────────────────────────────────────────────

# The one writable area in the languages sandbox (see languages/filesystem.rego
# — only /work is rw). The caller may override within /work.
DEFAULT_CANONICAL_PATH = os.environ.get("NANOCODEX_CRAFTOS_TURTLE_PATH", "/work/turtle.lua")
BOOTSTRAP_PATH = os.environ.get("NANOCODEX_LANGUAGES_BOOTSTRAP", "/opt/languages/bootstrap.js")

# The shared mcp-v8 both the codex thread and the validator address, keyed by a
# per-job fs label. Falls back to the remote-preset URL so a single languages
# mcp-v8 serves both. It MUST be a languages-configured mcp-v8 (craftos wasm +
# an fs store so /work persists across the label's run_js calls).
VALIDATOR_URL_ENV = "NANOCODEX_CRAFTOS_MCP_V8_URL"
VALIDATOR_TOKEN_ENV = "NANOCODEX_CRAFTOS_MCP_V8_TOKEN"

# V8 heap cap (MB) for validator run_js calls — bootstrap.js is ~7.4MB, so the
# 8MB default OOMs on compile (matches agui/sandbox.py's _WASM_HEAP_MEMORY_MAX_MB).
VALIDATOR_HEAP_MB = int(os.environ.get("NANOCODEX_CRAFTOS_VALIDATE_HEAP_MB", "256"))

DEFAULT_MAX_TURNS = int(os.environ.get("NANOCODEX_CRAFTOS_MAX_TURNS", "6"))
DEFAULT_TURN_TIMEOUT = float(os.environ.get("NANOCODEX_CRAFTOS_TURN_TIMEOUT", "600"))
VALIDATE_TIMEOUT = float(os.environ.get("NANOCODEX_CRAFTOS_VALIDATE_TIMEOUT", "120"))
# Hard ceiling on turns regardless of caller request, so a webhook can't ask
# for an unbounded run.
MAX_TURNS_CEILING = int(os.environ.get("NANOCODEX_CRAFTOS_MAX_TURNS_CEILING", "40"))

S3_BUCKET_ENV = "NANOCODEX_CRAFTOS_S3_BUCKET"
S3_PREFIX_ENV = "NANOCODEX_CRAFTOS_S3_PREFIX"
S3_ENDPOINT_ENV = "NANOCODEX_S3_ENDPOINT"  # e.g. a MinIO endpoint
S3_REGION_ENV = "NANOCODEX_S3_REGION"
S3_PRESIGN_TTL = int(os.environ.get("NANOCODEX_CRAFTOS_S3_PRESIGN_TTL", str(24 * 3600)))


# ── request model ───────────────────────────────────────────────────────────


@dataclass
class Budget:
    """How long the solve may run. `turns` always applies (and is clamped to
    MAX_TURNS_CEILING); `tokens` is enforced via codex's goal accounting when
    available; `seconds` is wall-clock."""

    turns: int = DEFAULT_MAX_TURNS
    tokens: Optional[int] = None
    seconds: Optional[float] = None

    @classmethod
    def parse(cls, raw: Any) -> "Budget":
        raw = raw or {}
        if not isinstance(raw, dict):
            raise ValueError("budget must be an object")
        turns = int(raw.get("turns", DEFAULT_MAX_TURNS))
        if turns < 1:
            raise ValueError("budget.turns must be >= 1")
        turns = min(turns, MAX_TURNS_CEILING)
        tokens = raw.get("tokens")
        tokens = int(tokens) if tokens is not None else None
        seconds = raw.get("seconds")
        seconds = float(seconds) if seconds is not None else None
        return cls(turns=turns, tokens=tokens, seconds=seconds)


@dataclass
class SolveRequest:
    """A webhook solve request.

    Fields (JSON body):
      sim            (required) a craftos() spec — `{timeout_ms?, nodes:[...]}`.
                     The target turtle node carries the world/world_lua with a
                     `test(sim)` post-condition; its `program` is what we solve
                     for (leave it absent or "").
      turtle_label   which node gets the program injected. Default: the sole
                     node that has a world/world_lua and no non-empty program.
      canonical_path where the model must write the program (must be under
                     /work). Default /work/turtle.lua.
      budget         {turns?, tokens?, seconds?}
      model          codex model override for the thread.
      instructions   extra task guidance appended to the solve prompt.
      s3             {bucket?, key?, endpoint_url?, region?} result target;
                     omitted fields come from env. When no bucket is resolvable
                     the bridge falls back to a local result store.
    """

    sim: dict
    target_index: int
    turtle_label: Optional[str]
    canonical_path: str
    budget: Budget
    model: Optional[str]
    instructions: Optional[str]
    s3: Optional[dict]

    @classmethod
    def parse(cls, body: Any) -> "SolveRequest":
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        sim = body.get("sim")
        if not isinstance(sim, dict):
            raise ValueError("`sim` is required and must be a craftos spec object")
        nodes = sim.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("`sim.nodes` must be a non-empty list")

        turtle_label = body.get("turtle_label")
        target_index = find_target_node(nodes, turtle_label)

        canonical_path = body.get("canonical_path") or DEFAULT_CANONICAL_PATH
        if not _is_work_path(canonical_path):
            raise ValueError("canonical_path must be under /work (the only writable area)")

        model = body.get("model")
        instructions = body.get("instructions")
        s3 = body.get("s3")
        if s3 is not None and not isinstance(s3, dict):
            raise ValueError("`s3` must be an object")
        return cls(
            sim=sim,
            target_index=target_index,
            turtle_label=turtle_label,
            canonical_path=canonical_path,
            budget=Budget.parse(body.get("budget")),
            model=model,
            instructions=instructions,
            s3=s3,
        )


def _is_work_path(path: str) -> bool:
    return isinstance(path, str) and path.startswith("/work/") and ".." not in path


def find_target_node(nodes: list, turtle_label: Optional[str]) -> int:
    """Index of the node whose `program` we solve for.

    With `turtle_label`, the node with that label. Otherwise the sole node that
    looks like a turtle (has world/world_lua) and has no non-empty program.
    Raises when the choice is ambiguous or empty so the caller fixes the spec.
    """
    if turtle_label is not None:
        for i, nd in enumerate(nodes):
            if isinstance(nd, dict) and nd.get("label") == turtle_label:
                return i
        raise ValueError(f"no node with label {turtle_label!r}")

    def is_turtle(nd: dict) -> bool:
        return nd.get("world") is not None or nd.get("world_lua") is not None

    def has_program(nd: dict) -> bool:
        return bool((nd.get("program") or "").strip())

    candidates = [
        i
        for i, nd in enumerate(nodes)
        if isinstance(nd, dict) and is_turtle(nd) and not has_program(nd)
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(
            "could not find a target node: expected exactly one turtle node "
            "(with `world`/`world_lua`) and no program. Set `turtle_label`."
        )
    raise ValueError(
        "ambiguous target: multiple turtle nodes without a program. Set `turtle_label` to pick one."
    )


# ── the validator ───────────────────────────────────────────────────────────


# Lua wrapper for the candidate: a stray top-level `return`/error in the
# model's program must NOT skip the craftos() postlude that runs world.test and
# emits SIM_RESULT, so run it inside a pcall'd function.
def _wrap_program(program_js_string: str) -> str:
    # Emitted into JS source; the value is a JS string expression.
    return f'"pcall(function()\\n" + {program_js_string} + "\\nend)"'


_SIM_LINE = re.compile(r"^\s*sim:\s*(\d+)\s+passed,\s*(\d+)\s+failed\s*$")
_RESULT_LINE = re.compile(r"^\s*SIM_RESULT:\s*(PASS|FAIL)\s*$")


def parse_sim_result(node_output: str) -> dict:
    """Authoritative pass/fail from a turtle node's emitted output.

    The craftos() postlude runs world.test(sim), emits the assertion log, then
    `sim: P passed, F failed` and `SIM_RESULT: PASS|FAIL`. We trust the LAST
    such pair and require PASS with failed==0 and passed>0 — so a program that
    prints a stray SIM_RESULT before the postlude cannot fake a pass.
    """
    lines = [ln for ln in (node_output or "").splitlines()]
    passed = failed = None
    result = None
    assertions: list[str] = []
    for ln in lines:
        m = _SIM_LINE.match(ln)
        if m:
            passed, failed = int(m.group(1)), int(m.group(2))
            continue
        m = _RESULT_LINE.match(ln)
        if m:
            result = m.group(1)
            continue
        s = ln.strip()
        if s.startswith("ok ") or s.startswith("ok-") or s.startswith("FAIL"):
            assertions.append(ln.rstrip())
    ok = result == "PASS" and failed == 0 and (passed or 0) > 0
    return {
        "passed": ok,
        "asserts_passed": passed,
        "asserts_failed": failed,
        "sim_result": result,
        "assertions": assertions,
    }


def build_validation_js(sim: dict, target_index: int, canonical_path: str) -> str:
    """JS run inside the shared-label mcp-v8: read the model's program from the
    canonical path, inject it (pcall-wrapped) as the target node's program, run
    the sim, and print `{program, output, error}` as JSON on stdout.
    """
    spec_json = json.dumps(sim)
    return f"""
(0,eval)(await fs.readFile({json.dumps(BOOTSTRAP_PATH)}, 'utf8'));
const CANON = {json.dumps(canonical_path)};
const IDX = {int(target_index)};
let prog = null, out = null, err = null;
try {{
  prog = await fs.readFile(CANON, 'utf8');
}} catch (e) {{
  console.log(JSON.stringify({{program: null, output: null, error: 'no_program: ' + String(e && e.message || e)}}));
}}
if (prog !== null) {{
  try {{
    const spec = {spec_json};
    spec.nodes[IDX].program = {_wrap_program("prog")};
    out = await craftos(spec);
  }} catch (e) {{
    err = String(e && e.message || e);
  }}
  console.log(JSON.stringify({{program: prog, output: out, error: err}}));
}}
""".strip()


@dataclass
class ValidationResult:
    program: Optional[str]
    passed: bool
    detail: dict
    craftos_output: Optional[dict]
    error: Optional[str]

    @property
    def has_program(self) -> bool:
        return self.program is not None


async def validate(
    runner: "McpV8Runner", label: str, req: SolveRequest, timeout: float = VALIDATE_TIMEOUT
) -> ValidationResult:
    """Run the sim with the model's actual `turtle.lua` injected and grade it."""
    code = build_validation_js(req.sim, req.target_index, req.canonical_path)
    stdout, run_err = await runner.run_js(label, code, timeout=timeout)
    if run_err and not stdout:
        return ValidationResult(None, False, {}, None, f"validator run_js error: {run_err}")
    payload = _last_json_line(stdout)
    if payload is None:
        return ValidationResult(
            None, False, {}, None, f"validator produced no JSON: {stdout[:400]!r}"
        )
    program = payload.get("program")
    if program is None:
        return ValidationResult(None, False, {}, None, payload.get("error") or "no_program")
    craftos_output = payload.get("output")
    if payload.get("error") or not isinstance(craftos_output, dict):
        return ValidationResult(
            program, False, {}, craftos_output, payload.get("error") or "sim did not run"
        )
    node = _find_output_node(craftos_output, req)
    detail = parse_sim_result(node.get("output", "") if node else "")
    return ValidationResult(program, detail["passed"], detail, craftos_output, None)


def _find_output_node(craftos_output: dict, req: SolveRequest) -> Optional[dict]:
    nodes = craftos_output.get("nodes") or []
    label = req.sim["nodes"][req.target_index].get("label")
    if label is not None:
        for nd in nodes:
            if nd.get("label") == label:
                return nd
    if req.target_index < len(nodes):
        return nodes[req.target_index]
    return nodes[0] if nodes else None


def _last_json_line(stdout: str) -> Optional[dict]:
    for ln in reversed((stdout or "").splitlines()):
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ── mcp-v8 runner (validator runtime) ───────────────────────────────────────


class McpV8Runner(Protocol):
    async def run_js(self, label: str, code: str, timeout: float) -> tuple[str, Optional[str]]:
        """Execute `code` on the mcp-v8 session identified by `label`, returning
        (stdout, error). `label` shares /work with the codex thread of the same
        label."""
        ...


class HttpMcpV8Runner:
    """Runs `run_js` on a streamable-HTTP mcp-v8, keyed per-session by the
    `X-MCP-Session-Id` header (the same header the `remote` sandbox preset uses
    for a thread). Same label ⇒ same /work as the codex thread."""

    def __init__(self, url: str, token: Optional[str] = None):
        self.url = url
        self.token = token

    def _headers(self, label: str) -> dict:
        h = {"X-MCP-Session-Id": label}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def run_js(self, label: str, code: str, timeout: float) -> tuple[str, Optional[str]]:
        # fastmcp is already a dependency (the FastMCP frontend). Its Client can
        # speak streamable HTTP to mcp-v8 and call the run_js tool.
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        transport = StreamableHttpTransport(url=self.url, headers=self._headers(label))
        # bootstrap.js is ~7.4MB of source; compiling it needs far more than the
        # mcp-v8 default 8MB V8 heap, so request the same 256MB the languages
        # sandbox preset uses (see agui/sandbox.py) or `(0,eval)(bootstrap)` OOMs.
        args = {
            "code": code,
            "heap_memory_max_mb": VALIDATOR_HEAP_MB,
            "execution_timeout_secs": int(timeout),
        }
        try:
            async with Client(transport) as client:
                result = await asyncio.wait_for(
                    client.call_tool("run_js", args), timeout=timeout + 10
                )
        except asyncio.TimeoutError:
            return "", f"validator timed out after {timeout}s"
        except Exception as e:  # noqa: BLE001 — surface any transport/tool error as a soft failure
            return "", f"validator transport error: {e}"
        return _extract_run_js(result)


def _extract_run_js(result: Any) -> tuple[str, Optional[str]]:
    """Pull (stdout, error) out of an mcp run_js tool result across fastmcp
    shapes (structured `{output, error}`, `.data`, or text content)."""
    for attr in ("data", "structured_content"):
        val = getattr(result, attr, None)
        if isinstance(val, dict) and ("output" in val or "error" in val):
            return str(val.get("output") or ""), (val.get("error") or None)
    # Text content blocks: mcp-v8 returns the JSON `{output, error}` as text.
    texts = []
    for block in getattr(result, "content", None) or []:
        t = getattr(block, "text", None)
        if t is not None:
            texts.append(t)
    joined = "\n".join(texts)
    try:
        obj = json.loads(joined)
        if isinstance(obj, dict) and ("output" in obj or "error" in obj):
            return str(obj.get("output") or ""), (obj.get("error") or None)
    except (ValueError, TypeError):
        pass
    return joined, None


def default_runner() -> Optional[McpV8Runner]:
    url = os.environ.get(VALIDATOR_URL_ENV) or os.environ.get("NANOCODEX_MCP_V8_URL")
    if not url:
        return None
    token = os.environ.get(VALIDATOR_TOKEN_ENV) or os.environ.get("NANOCODEX_MCP_V8_TOKEN")
    return HttpMcpV8Runner(url, token or None)


def solver_sandbox(label: str) -> SandboxSpec:
    """The codex thread's sandbox for a solve: the shared validator mcp-v8,
    keyed by this job's `label`, so the model's run_js writes to the same /work
    the validator (same label) reads. Requires a languages-configured shared
    mcp-v8 (craftos wasm + fs store)."""
    url = os.environ.get(VALIDATOR_URL_ENV) or os.environ.get("NANOCODEX_MCP_V8_URL")
    if not url:
        raise RuntimeError(
            f"{VALIDATOR_URL_ENV} (or NANOCODEX_MCP_V8_URL) must point at a "
            "languages-configured streamable-HTTP mcp-v8 for the CraftOS solver"
        )
    token = os.environ.get(VALIDATOR_TOKEN_ENV) or os.environ.get("NANOCODEX_MCP_V8_TOKEN")
    headers = {"X-MCP-Session-Id": label}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return SandboxSpec(
        raw={
            "url": url,
            "http_headers": headers,
            "startup_timeout_sec": 30,
            "tool_timeout_sec": 180,
            "default_tools_approval_mode": "approve",
        }
    )


# ── result stores (poll target) ─────────────────────────────────────────────


class ResultStore(Protocol):
    def poll_url(self, job_id: str, request: Optional[Request] = None) -> str: ...
    async def put(self, job_id: str, result: dict) -> None: ...
    async def get(self, job_id: str) -> Optional[dict]: ...


class S3ResultStore:
    """Writes the result JSON to `s3://bucket/prefix/job_id.json` and hands the
    caller a presigned GET URL to poll."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        endpoint_url: Optional[str] = None,
        region: Optional[str] = None,
    ):
        import boto3  # lazy: boto3 is an optional dependency

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3", endpoint_url=endpoint_url or None, region_name=region or None
        )

    def _key(self, job_id: str) -> str:
        return f"{self.prefix}/{job_id}.json" if self.prefix else f"{job_id}.json"

    def poll_url(self, job_id: str, request: Optional[Request] = None) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(job_id)},
            ExpiresIn=S3_PRESIGN_TTL,
        )

    async def put(self, job_id: str, result: dict) -> None:
        body = json.dumps(result, indent=2).encode()
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket,
            Key=self._key(job_id),
            Body=body,
            ContentType="application/json",
        )

    async def get(self, job_id: str) -> Optional[dict]:
        try:
            obj = await asyncio.to_thread(
                self._client.get_object, Bucket=self.bucket, Key=self._key(job_id)
            )
        except Exception:  # noqa: BLE001 — missing key etc.
            return None
        return json.loads(obj["Body"].read())


class LocalResultStore:
    """In-bridge fallback when S3 isn't configured: results held in memory and
    served from `GET /agui/craftos/result/{job_id}` (404 until the job ends)."""

    def __init__(self):
        self._results: dict[str, dict] = {}

    def poll_url(self, job_id: str, request: Optional[Request] = None) -> str:
        base = str(request.base_url).rstrip("/") if request is not None else ""
        return f"{base}/agui/craftos/result/{job_id}"

    async def put(self, job_id: str, result: dict) -> None:
        self._results[job_id] = result

    async def get(self, job_id: str) -> Optional[dict]:
        return self._results.get(job_id)


def make_store(req_s3: Optional[dict]) -> ResultStore:
    """S3 store when a bucket is resolvable (request `s3.bucket` or env), else
    the local fallback."""
    s3 = dict(req_s3 or {})
    bucket = s3.get("bucket") or os.environ.get(S3_BUCKET_ENV)
    if not bucket:
        return LocalResultStore()
    return S3ResultStore(
        bucket=bucket,
        prefix=s3.get("prefix") or os.environ.get(S3_PREFIX_ENV, ""),
        endpoint_url=s3.get("endpoint_url") or os.environ.get(S3_ENDPOINT_ENV),
        region=s3.get("region") or os.environ.get(S3_REGION_ENV),
    )


# ── token/goal accounting ────────────────────────────────────────────────────


class TokenMeter:
    """Best-effort token accounting from `thread/tokenUsage/updated` events,
    used when codex's goal accounting isn't available. codex reports usage in
    several shapes across versions, so we defensively pick the largest
    total-like number we see."""

    def __init__(self):
        self.total = 0

    def observe(self, method: str, params: dict) -> None:
        if method != "thread/tokenUsage/updated":
            return
        self.total = max(self.total, _extract_total_tokens(params))


def _extract_total_tokens(params: dict) -> int:
    best = 0

    def walk(v: Any):
        nonlocal best
        if isinstance(v, dict):
            for k, val in v.items():
                if isinstance(val, (int, float)) and re.search(
                    r"total.*token|token.*total", k, re.I
                ):
                    best = max(best, int(val))
                walk(val)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(params)
    if best:
        return best
    # No explicit "total*token" field: sum input/output-ish leaf counts.
    s = 0

    def sum_leaves(v: Any):
        nonlocal s
        if isinstance(v, dict):
            for k, val in v.items():
                if isinstance(val, (int, float)) and re.search(r"token", k, re.I):
                    s += int(val)
                else:
                    sum_leaves(val)
        elif isinstance(v, list):
            for x in v:
                sum_leaves(x)

    sum_leaves(params)
    return s


# ── the solve loop ───────────────────────────────────────────────────────────

_SOLVE_PREAMBLE = (
    "You are solving a ComputerCraft (CC:Tweaked) turtle challenge in a CraftOS "
    "simulation. Your ONLY job is to write ONE Lua turtle program to the file "
    "`{path}` (in your /work sandbox) that makes the simulation pass.\n\n"
    "How you are graded (this runs automatically after every turn — you do NOT "
    "run it): the harness takes the sim spec below, injects YOUR `{path}` as the "
    "turtle node's program, runs it, and runs the world's `test(sim)` "
    "post-condition. You pass only when it emits `SIM_RESULT: PASS` "
    "(`sim: N passed, 0 failed`). You never see or control the world/test — only "
    "the program.\n\n"
    "The sim spec (the target node's `program` is intentionally blank — that is "
    "what you write to `{path}`):\n```json\n{sim}\n```\n\n"
    "Work iteratively with the craftos-sim skill: read "
    "`/opt/languages/skills/craftos-sim/SKILL.md` and `.../cc-tweaked/SKILL.md`, "
    "and in a run_js call load the bootstrap and dry-run your own copy of the "
    "sim to inspect turtle state before writing the file. Then write your best "
    "program to `{path}` with fs.writeFile and end your turn. Do not claim "
    "success — the harness decides.\n"
)


def build_initial_prompt(req: SolveRequest) -> str:
    sim_json = json.dumps(req.sim, indent=2)
    prompt = _SOLVE_PREAMBLE.format(path=req.canonical_path, sim=sim_json)
    if req.instructions:
        prompt += f"\nAdditional guidance from the requester:\n{req.instructions}\n"
    return prompt


def build_feedback_prompt(
    req: SolveRequest, v: ValidationResult, turn: int, turns_left: int
) -> str:
    if not v.has_program:
        return (
            f"The file `{req.canonical_path}` does not exist yet (or was empty). "
            f"Write your turtle program to `{req.canonical_path}` with "
            f"fs.writeFile, then end your turn. ({turns_left} attempt(s) left.)"
        )
    lines = "\n".join(v.detail.get("assertions") or []) or "(the sim produced no assertion output)"
    tail = ""
    if v.error:
        tail = f"\nThe sim reported an error running your program: {v.error}"
    return (
        f"Your `{req.canonical_path}` did NOT pass (attempt {turn}). The harness "
        f"ran the sim with your program and the world's test post-condition and "
        f"got:\n```\n{lines}\nSIM_RESULT: FAIL\n```{tail}\n\n"
        f"Diagnose why (dry-run the sim yourself with craftos() in run_js and "
        f"inspect sim.pos()/sim.inventory()/sim.block(...)), fix the program, and "
        f"rewrite `{req.canonical_path}`. {turns_left} attempt(s) left."
    )


async def solve_job(
    req: SolveRequest,
    job_id: str,
    store: ResultStore,
    runner: McpV8Runner,
    *,
    nc_factory=None,
    turn_timeout: float = DEFAULT_TURN_TIMEOUT,
) -> dict:
    """Run the validation loop to completion and upload the result. Never
    raises — any failure is recorded as an error result so the poll always
    resolves. Returns the result dict (also for tests)."""
    label = f"craftos-{job_id}"
    started = time.monotonic()
    meter = TokenMeter()
    goal_ok = False
    result: dict

    def on_event(method: str, params: dict):
        meter.observe(method, params)

    try:
        nc = await (nc_factory() if nc_factory else Nanocodex.connect())
    except Exception as e:  # noqa: BLE001
        result = _error_result(req, job_id, f"could not connect to codex: {e}", turns=0, tokens=0)
        await store.put(job_id, result)
        return result

    try:
        created = await nc.create_thread(
            sandbox=solver_sandbox(label),
            model=req.model,
            # codex's cwd is on the app-server host, not the mcp-v8 sandbox fs
            # (the model has no fs tools anyway — /work lives inside run_js), so
            # match the rest of the bridge and use /tmp.
            cwd="/tmp",
            # We build the thread sandbox directly (a remote-by-label spec), so
            # the languages capability addendum isn't auto-appended — pass it so
            # the model knows about /work, bootstrap.js, craftos() and the
            # bundled skills.
            developer_instructions=_LANGUAGES_INSTRUCTIONS,
        )
        thread_id = created["thread"]["id"]

        # Native budget: set an objective + tokenBudget goal (best-effort).
        if req.budget.tokens is not None:
            goal_ok = await _set_goal(nc, thread_id, req)

        feedback = build_initial_prompt(req)
        last_v: Optional[ValidationResult] = None
        turns_used = 0
        for turn in range(1, req.budget.turns + 1):
            if req.budget.seconds is not None and (time.monotonic() - started) > req.budget.seconds:
                break
            turns_used = turn
            try:
                await nc.run_turn(thread_id, feedback, timeout=turn_timeout, on_event=on_event)
            except Exception as e:  # noqa: BLE001 — a failed turn is not fatal to the job
                last_v = ValidationResult(None, False, {}, None, f"turn error: {e}")
                feedback = (
                    f"The previous turn failed ({e}). Please retry: write your "
                    f"turtle program to `{req.canonical_path}` and end your turn."
                )
                continue

            tokens_used = await _tokens_used(nc, thread_id, goal_ok, meter)

            # The "stop hook": validate the ACTUAL file the model wrote.
            v = await validate(runner, label, req)
            last_v = v
            if v.passed:
                result = {
                    "status": "ok",
                    "job_id": job_id,
                    "program": v.program,
                    "canonical_path": req.canonical_path,
                    "turns": turn,
                    "tokens_used": tokens_used,
                    "sim_result": "PASS",
                    "assertions": v.detail.get("assertions"),
                    "sim_output": v.craftos_output,
                }
                await _finish_goal(nc, thread_id, goal_ok, "achieved")
                await store.put(job_id, result)
                return result

            if req.budget.tokens is not None and tokens_used >= req.budget.tokens:
                break

            turns_left = req.budget.turns - turn
            feedback = build_feedback_prompt(req, v, turn, turns_left)

        # Budget exhausted without a pass.
        tokens_used = await _tokens_used(nc, thread_id, goal_ok, meter)
        result = _error_result(
            req,
            job_id,
            "budget exhausted before SIM_RESULT: PASS",
            turns=turns_used,
            tokens=tokens_used,
            last_v=last_v,
        )
        await _finish_goal(nc, thread_id, goal_ok, "abandoned")
        await store.put(job_id, result)
        return result
    except Exception as e:  # noqa: BLE001 — last-ditch: always record something to poll
        result = _error_result(req, job_id, f"solver crashed: {e}", turns=0, tokens=0)
        await store.put(job_id, result)
        return result
    finally:
        try:
            await nc.close()
        except Exception:  # noqa: BLE001
            pass


def _error_result(
    req: SolveRequest,
    job_id: str,
    reason: str,
    *,
    turns: int,
    tokens: int,
    last_v: Optional[ValidationResult] = None,
) -> dict:
    out: dict = {
        "status": "error",
        "job_id": job_id,
        "reason": reason,
        "canonical_path": req.canonical_path,
        "turns": turns,
        "tokens_used": tokens,
    }
    if last_v is not None:
        out["last_program"] = last_v.program
        out["last_assertions"] = last_v.detail.get("assertions")
        out["last_sim_result"] = last_v.detail.get("sim_result")
        if last_v.error:
            out["last_error"] = last_v.error
        out["sim_output"] = last_v.craftos_output
    return out


async def _set_goal(nc: Nanocodex, thread_id: str, req: SolveRequest) -> bool:
    """Set the thread goal + tokenBudget. Best-effort — returns whether it stuck
    so we know if goal-based accounting is available on this codex."""
    objective = (
        f"Write a CraftOS turtle program to {req.canonical_path} that makes the "
        "provided simulation emit SIM_RESULT: PASS."
    )[:4000]
    params: dict = {"threadId": thread_id, "objective": objective, "status": "active"}
    if req.budget.tokens is not None:
        params["tokenBudget"] = int(req.budget.tokens)
    try:
        await nc.request("thread/goal/set", params)
        return True
    except (RpcError, Exception):  # noqa: BLE001 — fork may lack the goal API
        return False


async def _finish_goal(nc: Nanocodex, thread_id: str, goal_ok: bool, status: str) -> None:
    if not goal_ok:
        return
    # Terminal status enum isn't guaranteed across forks; try, then fall back to
    # clearing the goal. Failures here never affect the job result.
    for attempt in ({"threadId": thread_id, "status": status}, None):
        try:
            if attempt is None:
                await nc.request("thread/goal/clear", {"threadId": thread_id})
            else:
                await nc.request("thread/goal/set", attempt)
            return
        except Exception:  # noqa: BLE001
            continue


async def _tokens_used(nc: Nanocodex, thread_id: str, goal_ok: bool, meter: TokenMeter) -> int:
    """Prefer codex's goal accounting (tokensUsed); fall back to the meter."""
    if goal_ok:
        try:
            resp = await nc.request("thread/goal/get", {"threadId": thread_id})
            goal = (resp or {}).get("goal") or {}
            used = goal.get("tokensUsed")
            if isinstance(used, (int, float)):
                return int(used)
        except Exception:  # noqa: BLE001
            pass
    return meter.total


# ── router ───────────────────────────────────────────────────────────────────

craftos_router = APIRouter()

# One process-wide local store instance so the fallback poll endpoint can find
# results across requests.
_local_store = LocalResultStore()
# Live/finished jobs, so a re-poll of the status endpoint works.
_jobs: dict[str, dict] = {}


def _store_for(req_s3: Optional[dict]) -> ResultStore:
    store = make_store(req_s3)
    return _local_store if isinstance(store, LocalResultStore) else store


@craftos_router.post("/agui/craftos/solve")
async def craftos_solve(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="body must be JSON")
    try:
        req = SolveRequest.parse(body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    runner = default_runner()
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{VALIDATOR_URL_ENV} (or NANOCODEX_MCP_V8_URL) is not set; the "
                "CraftOS solver needs a shared languages mcp-v8 to validate against."
            ),
        )
    try:
        store = _store_for(req.s3)
    except Exception as e:  # noqa: BLE001 — e.g. boto3 missing / bad S3 config
        raise HTTPException(status_code=503, detail=f"result store unavailable: {e}")

    job_id = uuid.uuid4().hex
    try:
        poll_url = store.poll_url(job_id, request)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not presign poll url: {e}")

    _jobs[job_id] = {"status": "running", "poll_url": poll_url}

    async def _run():
        try:
            res = await solve_job(req, job_id, store, runner)
            _jobs[job_id] = {"status": res.get("status", "done"), "poll_url": poll_url}
        except Exception as e:  # noqa: BLE001
            _jobs[job_id] = {"status": "error", "error": str(e), "poll_url": poll_url}

    asyncio.create_task(_run())

    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "poll_url": poll_url,
            "status_url": f"{str(request.base_url).rstrip('/')}/agui/craftos/jobs/{job_id}",
            "budget": {
                "turns": req.budget.turns,
                "tokens": req.budget.tokens,
                "seconds": req.budget.seconds,
            },
            "canonical_path": req.canonical_path,
        },
    )


@craftos_router.get("/agui/craftos/jobs/{job_id}")
async def craftos_job_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return job


@craftos_router.get("/agui/craftos/result/{job_id}")
async def craftos_local_result(job_id: str):
    """Poll target for the local (no-S3) fallback store. 404 until the job ends."""
    result = await _local_store.get(job_id)
    if result is None:
        # 202 = accepted, still running / not found yet — callers poll on 202.
        return Response(
            status_code=202,
            content=json.dumps({"status": "pending"}),
            media_type="application/json",
        )
    return JSONResponse(content=result)
