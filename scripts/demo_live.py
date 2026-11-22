#!/usr/bin/env python
"""Live rehearsal driver for GameConfig Copilot (manual run, real API + real model).

用法
----
    python scripts/demo_live.py                     # 全流程：生成 → 确认 → 写盘
    python scripts/demo_live.py --skip-confirm      # 只预览，不确认（零写入）
    python scripts/demo_live.py --base-url http://127.0.0.1:8001
    python scripts/demo_live.py --timeout 300       # 模型较慢时放宽 SSE 读取超时

前提
----
* API 已启动，并且是 **写模式**（PowerShell 同一窗口先 ``$env:GCC_MCP_MODE="write"``）。
  默认 ``readonly`` 时 ``/confirm`` 返回 403 ``E_WRITE_DISABLED``。
* 生成依赖真实模型：``GCC_LLM_API_KEY`` 已通过环境变量或 ``.env`` 提供。
* 检索索引已构建：``gcc ingest --source data``（产物 ``out/index``）。
* 从仓库根目录启动 uvicorn（服务端按相对路径写 ``data/tasks``）。

流程
----
1. ``POST /api/v1/sessions`` 建会话；
2. 第 1 幕：正常需求 → SSE → ``awaiting_confirmation`` → ``/confirm``；
3. 第 2 幕：重述需求并把奖励改成 150 金币 → 同上；
4. 第 3 幕：反例「1 级任务 1000 金币」→ 确认时被 ``E_REWARD_RANGE`` 拦下（零写入）；
5. 打印每幕耗时与总耗时。

脚本本身不读写 ``data/``；所有写盘都经由 API 的确认接口，在写模式 + 校验通过后执行。
异常按幕捕获，单幕失败不会中断整场。退出码：0 全部符合预期；1 某幕不符预期或抛异常；
2 环境/健康检查/建会话失败。

本脚本已在沙箱内通过语法与 ruff 校验，但**未实际运行**（沙箱不能起 API、不能写 data/）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_DESIGNER = "u_1024"

TASK_MESSAGE = "给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石，奖励别太高"
TASK_MESSAGE_150 = "给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石，奖励改成 150 金币"
BAD_MESSAGE = "给新手村 1 级任务加 1000 金币奖励"

_RETRYABLE_STATUS = {429, 502, 503, 504}


@dataclass(frozen=True)
class ActSpec:
    """One rehearsed scene: request text plus the expected confirm outcome."""

    title: str
    message: str
    expected: str  # "committed" (happy path) or "rejected" (counter-example)


ACTS: tuple[ActSpec, ...] = (
    ActSpec(title="第 1 幕 · 正常生成", message=TASK_MESSAGE, expected="committed"),
    ActSpec(title="第 2 幕 · 奖励改 150", message=TASK_MESSAGE_150, expected="committed"),
    ActSpec(title="第 3 幕 · 反例 1000 金币", message=BAD_MESSAGE, expected="rejected"),
)


class ApiError(RuntimeError):
    """Non-200 response from the API (usually a JSON ``detail`` envelope)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Turn:
    """Collected SSE events for one request (message or confirm)."""

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    awaiting: dict[str, Any] | None = None
    trace_id: str = ""
    status: str = ""

    def first(self, name: str) -> dict[str, Any] | None:
        for event_name, data in self.events:
            if event_name == name:
                return data
        return None


@dataclass
class ActResult:
    """Outcome of one act, used by the final summary and the exit code."""

    title: str
    outcome: str
    expected: str
    elapsed: float = 0.0
    trace_id: str = ""
    confirm_trace_id: str = ""
    detail: str = ""


# --------------------------------------------------------------------- helpers
def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _short(value: Any, limit: int = 100) -> str:
    text = value if isinstance(value, str) else _json_compact(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _loads(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {"_raw": payload}
    return value if isinstance(value, dict) else {"_value": value}


def _error_detail(response: httpx.Response) -> tuple[str, str]:
    """Return ``(code, message)`` from an error response, tolerating plain text."""

    try:
        body = response.json()
    except ValueError:
        return f"HTTP_{response.status_code}", _short(response.text, 300)
    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, dict):
        return (
            str(detail.get("code") or f"HTTP_{response.status_code}"),
            str(detail.get("message") or detail),
        )
    return f"HTTP_{response.status_code}", _short(detail, 300)


def _retry_wait(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        wait = float(raw)
    except ValueError:
        wait = float(attempt * 2)
    return min(max(wait, 1.0), 10.0)


def _open_stream(
    client: httpx.Client,
    path: str,
    payload: dict[str, Any],
    attempts: int = 3,
) -> httpx.Response:
    """Open an SSE response, retrying only rate-limited/unavailable responses."""

    for attempt in range(1, attempts + 1):
        request = client.build_request("POST", path, json=payload)
        response = client.send(request, stream=True)
        if response.status_code not in _RETRYABLE_STATUS or attempt == attempts:
            return response
        wait = _retry_wait(response, attempt)
        print(f"  [retry] HTTP {response.status_code}，{wait:.0f}s 后重试（第 {attempt} 次）")
        response.close()
        time.sleep(wait)
    raise ApiError(0, "E_RETRY", "重试次数耗尽")


def _iter_sse(response: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse ``event:`` / ``data:`` frames from a streaming response."""

    event = ""
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if event or data_lines:
                yield event or "message", _loads("\n".join(data_lines))
            event, data_lines = "", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if event or data_lines:
        yield event or "message", _loads("\n".join(data_lines))


def _print_diff(diff: Any) -> None:
    if not isinstance(diff, dict):
        return
    stats = diff.get("stats")
    if isinstance(stats, dict):
        print(f"      diff: +{stats.get('added', 0)} / -{stats.get('removed', 0)}")
        lines = str(diff.get("unified_diff") or "").splitlines()
        for line in lines[:12]:
            print(f"        {line}")
        if len(lines) > 12:
            print(f"        ...（diff 共 {len(lines)} 行）")
        return
    if diff.get("created"):
        print("      diff: 新建文件（无旧版本）")
        return
    changed = diff.get("changed")
    changed = changed if isinstance(changed, list) else []
    print(f"      diff: changed={diff.get('change_count', len(changed))}")
    for item in changed[:6]:
        if not isinstance(item, dict):
            continue
        print(
            f"        {item.get('path')}: "
            f"{_short(item.get('before'), 40)} -> {_short(item.get('after'), 40)}"
        )


def _print_event(name: str, data: dict[str, Any], tag: str = "") -> None:
    """Print one SSE event with only the fields that matter on camera."""

    prefix = f"  {tag}"
    if name == "meta":
        print(f"{prefix}event meta        trace_id={data.get('trace_id')}")
    elif name == "status":
        print(f"{prefix}event status      {data.get('stage')} | {data.get('label', '')}")
        if data.get("stage") == "answered" and data.get("answer"):
            print(f"{prefix}                  answer={_short(data.get('answer'), 100)}")
    elif name == "evidence":
        items = data.get("items")
        items = items if isinstance(items, list) else []
        sources = [str(item.get("source", "")) for item in items[:3] if isinstance(item, dict)]
        print(f"{prefix}event evidence    {len(items)} 条 | " + " ; ".join(sources))
    elif name == "draft":
        draft = data.get("draft")
        draft = draft if isinstance(draft, dict) else {}
        level_range = draft.get("level_range")
        if isinstance(level_range, dict):
            level = f"{level_range.get('min')}-{level_range.get('max')}"
        else:
            level = str(draft.get("level") or "-")
        task_type = draft.get("task_type") or draft.get("type") or "-"
        print(
            f"{prefix}event draft       task_id={draft.get('task_id')} "
            f"title={_short(draft.get('title'), 40)}"
        )
        print(
            f"{prefix}                  level={level} "
            f"type={task_type} rewards={_short(draft.get('rewards'), 90)}"
        )
    elif name == "validation":
        errors = data.get("errors")
        errors = errors if isinstance(errors, list) else []
        warnings = data.get("warnings")
        warnings = warnings if isinstance(warnings, list) else []
        print(
            f"{prefix}event validation  ok={data.get('ok')} "
            f"errors={len(errors)} warnings={len(warnings)}"
        )
        for error in errors:
            if not isinstance(error, dict):
                continue
            print(
                f"{prefix}                  {error.get('code')} "
                f"{error.get('field')} "
                f"constraint={error.get('constraint_ref')} "
                f"| {_short(error.get('message'), 80)}"
            )
    elif name == "awaiting_confirmation":
        print(
            f"{prefix}event awaiting_confirmation "
            f"draft_id={data.get('draft_id')} "
            f"target_path={data.get('target_path')} "
            f"expires_in={data.get('expires_in')}"
        )
        print(f"{prefix}                  commit_token={_short(data.get('commit_token'), 24)}")
        _print_diff(data.get("diff"))
    elif name == "committed":
        print(
            f"{prefix}event committed   commit_id={data.get('commit_id')} "
            f"backup={data.get('backup')} revision={_short(data.get('revision'), 24)}"
        )
    elif name == "warning":
        print(f"{prefix}event warning     {data.get('code')} | {_short(data.get('message'), 100)}")
    elif name == "error":
        print(
            f"{prefix}event error       {data.get('code')} "
            f"stage={data.get('stage')} | {_short(data.get('message'), 120)}"
        )
    elif name == "done":
        print(
            f"{prefix}event done        status={data.get('status')} "
            f"elapsed_ms={data.get('elapsed_ms')} "
            f"tokens={data.get('tokens')} cost_cny={data.get('cost_cny')}"
        )
    else:
        print(f"{prefix}event {name:<11} {_short(data, 120)}")


def _run_stream(
    client: httpx.Client,
    path: str,
    payload: dict[str, Any],
    *,
    tag: str = "",
) -> Turn:
    """POST an SSE endpoint, print every event, and return the collected turn."""

    turn = Turn()
    response = _open_stream(client, path, payload)
    try:
        if response.status_code != 200:
            code, message = _error_detail(response)
            raise ApiError(response.status_code, code, message)
        for name, data in _iter_sse(response):
            turn.events.append((name, data))
            if name == "meta":
                turn.trace_id = str(data.get("trace_id") or "")
            elif name == "awaiting_confirmation":
                turn.awaiting = data
            elif name == "done":
                turn.status = str(data.get("status") or "")
            _print_event(name, data, tag=tag)
    finally:
        response.close()
    return turn


def _check_health(client: httpx.Client) -> dict[str, Any] | None:
    try:
        response = client.get("/healthz")
    except httpx.HTTPError as exc:
        print(f"[错误] 无法连接 {client.base_url}healthz：{exc}")
        print("       先在另一个 PowerShell 窗口启动 API（见 docs/demo-rehearsal.md 第②节）。")
        return None
    if response.status_code != 200:
        print(f"[错误] /healthz 返回 HTTP {response.status_code}：{_short(response.text, 160)}")
        return None
    try:
        data = response.json()
    except ValueError:
        print("[错误] /healthz 返回的不是 JSON。")
        return None
    if not isinstance(data, dict):
        print("[错误] /healthz 返回结构异常。")
        return None
    mcp = data.get("mcp") if isinstance(data.get("mcp"), dict) else {}
    print(
        f"[health] status={data.get('status')} mode={mcp.get('mode')} "
        f"connected={mcp.get('connected')} graph_ready={data.get('graph_ready')} "
        f"sessions={data.get('sessions')}"
    )
    return data


def _create_session(client: httpx.Client, designer_id: str) -> str:
    response = client.post("/api/v1/sessions", json={"designer_id": designer_id})
    if response.status_code != 201:
        code, message = _error_detail(response)
        raise ApiError(response.status_code, code, message)
    data = response.json()
    session_id = str(data.get("session_id") or "") if isinstance(data, dict) else ""
    if not session_id:
        raise ApiError(response.status_code, "E_SESSION", "响应缺少 session_id")
    return session_id


# ------------------------------------------------------------------------- acts
def _run_act(
    client: httpx.Client,
    session_id: str,
    spec: ActSpec,
    *,
    confirm: bool,
    note: str,
) -> ActResult:
    print(f"\n--- {spec.title} ---")
    print(f"需求: {spec.message}")
    turn = _run_stream(
        client,
        "/api/v1/messages",
        {"session_id": session_id, "message": spec.message},
    )

    if turn.awaiting is None:
        detail = f"未收到 awaiting_confirmation（done={turn.status or '无'}）"
        print(f"[结果] {detail}")
        return ActResult(
            title=spec.title,
            outcome=turn.status or "no-draft",
            expected=spec.expected,
            trace_id=turn.trace_id,
            detail=detail,
        )

    awaiting = turn.awaiting
    if not confirm:
        detail = f"停在预览（{awaiting.get('target_path')}），未写盘"
        print("[skip-confirm] 已停在预览，不调用 /confirm。")
        return ActResult(
            title=spec.title,
            outcome="preview-only",
            expected=spec.expected,
            trace_id=turn.trace_id,
            detail=detail,
        )

    draft_id = str(awaiting.get("draft_id") or "")
    token = str(awaiting.get("commit_token") or "")
    print(f"[confirm] POST /api/v1/drafts/{draft_id}/confirm")
    confirm_turn = _run_stream(
        client,
        f"/api/v1/drafts/{draft_id}/confirm",
        {"commit_token": token, "note": note},
        tag="[confirm] ",
    )

    committed = confirm_turn.first("committed")
    if committed is not None:
        detail = (
            f"commit_id={committed.get('commit_id')} "
            f"backup={committed.get('backup')} "
            f"revision={_short(committed.get('revision'), 16)}"
        )
        print(f"[结果] 已写入：{detail}")
        return ActResult(
            title=spec.title,
            outcome="committed",
            expected=spec.expected,
            trace_id=turn.trace_id,
            confirm_trace_id=confirm_turn.trace_id,
            detail=detail,
        )

    error = confirm_turn.first("error")
    code = str((error or {}).get("code") or confirm_turn.status or "unknown")
    message = str((error or {}).get("message") or "")
    if confirm_turn.status == "rejected":
        if spec.expected == "rejected":
            print(f"[零写入] 被拒绝（{code}）：{message}")
            print("         未收到 committed 事件；拒绝发生在写盘前，data/tasks 无变化。")
        else:
            print(f"[结果] 被拒绝（{code}）：{message}")
        return ActResult(
            title=spec.title,
            outcome="rejected",
            expected=spec.expected,
            trace_id=turn.trace_id,
            confirm_trace_id=confirm_turn.trace_id,
            detail=f"{code}: {_short(message, 80)}",
        )
    detail = f"确认未提交（status={confirm_turn.status or '无'}, code={code}）"
    print(f"[结果] {detail}")
    return ActResult(
        title=spec.title,
        outcome=confirm_turn.status or "unknown",
        expected=spec.expected,
        trace_id=turn.trace_id,
        confirm_trace_id=confirm_turn.trace_id,
        detail=detail,
    )


# ------------------------------------------------------------------------ main
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GameConfig Copilot live 排练：生成 → 预览 → 确认 → 反例。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python scripts/demo_live.py\n"
            "  python scripts/demo_live.py --skip-confirm\n"
            "  python scripts/demo_live.py --base-url http://127.0.0.1:8001 --timeout 300\n"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"API 根地址（默认 {DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--skip-confirm",
        action="store_true",
        help="只预览，不调用 /confirm（零写入，也看不到确认时的业务校验）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="SSE 读取超时秒数（默认 180；模型慢时调大）",
    )
    parser.add_argument(
        "--designer",
        default=DEFAULT_DESIGNER,
        help=f"会话的 designer_id（默认 {DEFAULT_DESIGNER}）",
    )
    return parser.parse_args(argv)


def _print_summary(
    results: list[ActResult],
    total: float,
    session_id: str,
    *,
    skip_confirm: bool,
) -> None:
    print(f"\n=== 总览（总耗时 {total:.1f}s） ===")
    for result in results:
        expected = "preview-only" if skip_confirm else result.expected
        mark = "OK" if result.outcome == expected else "!!"
        print(
            f"  [{mark}] {result.title:<20} {result.outcome:<13} "
            f"{result.elapsed:5.1f}s  {result.detail}"
        )
    print(f"  会话: {session_id}")
    traces = [r for r in results if r.trace_id or r.confirm_trace_id]
    if traces:
        print("  轨迹（GET /api/v1/traces/{trace_id}）:")
        for result in traces:
            ids = "  ".join(x for x in (result.trace_id, result.confirm_trace_id) if x)
            print(f"    {result.title}: {ids}")
    if skip_confirm:
        print(
            "  [提示] --skip-confirm：全部停在预览；"
            "E_REWARD_RANGE 只在确认复检时触发，本模式看不到。"
        )


def _exit_code(results: list[ActResult], *, skip_confirm: bool) -> int:
    if skip_confirm:
        return 0 if all(r.outcome == "preview-only" for r in results) else 1
    return 0 if all(r.outcome == r.expected for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(errors="backslashreplace")

    if args.timeout <= 0:
        print("[错误] --timeout 必须 > 0")
        return 2

    base_url = args.base_url.rstrip("/")
    print(f"GameConfig Copilot · live 排练  base_url={base_url}")
    if args.skip_confirm:
        print("[模式] --skip-confirm：只预览，不确认（不会写盘）")

    timeout = httpx.Timeout(args.timeout, connect=10.0)
    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        health = _check_health(client)
        if health is None:
            return 2

        mcp = health.get("mcp") if isinstance(health.get("mcp"), dict) else {}
        if mcp.get("mode") != "write":
            print("[错误] 写模式未开启：healthz.mcp.mode != write。")
            print('       同一 PowerShell 窗口先执行 $env:GCC_MCP_MODE="write"，再重启 uvicorn；')
            print("       否则 /confirm 会返回 403 E_WRITE_DISABLED。")
            if not args.skip_confirm:
                return 2
            print("[提示] 已指定 --skip-confirm，继续只看预览。")

        try:
            session_id = _create_session(client, args.designer)
        except (httpx.HTTPError, ApiError) as exc:
            print(f"[错误] 创建会话失败：{exc}")
            return 2
        print(f"[session] {session_id}")

        started = time.perf_counter()
        results: list[ActResult] = []
        for spec in ACTS:
            act_started = time.perf_counter()
            try:
                result = _run_act(
                    client,
                    session_id,
                    spec,
                    confirm=not args.skip_confirm,
                    note="demo_live",
                )
            except Exception as exc:  # noqa: BLE001 - one act must not abort the rehearsal
                result = ActResult(
                    title=spec.title,
                    outcome="error",
                    expected=spec.expected,
                    detail=f"{type(exc).__name__}: {exc}",
                )
                print(f"[异常] {spec.title} 中断：{type(exc).__name__}: {exc}")
                print("       已跳过本幕，继续下一幕。")
            result.elapsed = time.perf_counter() - act_started
            results.append(result)

        total = time.perf_counter() - started
        _print_summary(results, total, session_id, skip_confirm=args.skip_confirm)
        return _exit_code(results, skip_confirm=args.skip_confirm)


if __name__ == "__main__":
    raise SystemExit(main())
