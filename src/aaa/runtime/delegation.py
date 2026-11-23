"""Bind local delegation tools to a host's model and permission scope."""

import logging
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import ContextVar
from time import monotonic
from uuid import uuid4

from agno.agent import Agent
from agno.run import RunContext
from agno.run.cancel import is_cancelled
from agno.team import Team, TeamMode
from agno.tools.function import Function
from agno.workflow import Condition, Parallel, Step, Workflow

from ..boundary.policy import REVIEW_FILE_TOOLS, needs_approval, signature
from ..model.catalog import Resolved
from .delegation_registry import DelegationRegistry, encode, validate

SAVE_TOOL = "save_delegation"
# Only the outer host's interactive HITL handler can grant a write for this run.
save_approvals: ContextVar[dict[str, int] | None] = ContextVar("delegation_save_approvals", default=None)
parent_run_id: ContextVar[str | None] = ContextVar("delegation_parent_run_id", default=None)
_logger = logging.getLogger(__name__)


def _owner(context):
    return (context.user_id or "", context.session_id)


def _functions(tools):
    return {fn.name: fn for tool in tools for fn in (
        tool.functions.values() if hasattr(tool, "functions") else
        (tool if isinstance(tool, Function) else Function.from_callable(tool),)
    )}


def _rejection(name, strategy, reasons):
    def check(function_call, arguments):
        if name in REVIEW_FILE_TOOLS or needs_approval(name, arguments, strategy):
            reason = f"{name} 需要用户确认；子任务不执行该动作"
            reasons.append(reason)
            return reason
        return function_call(**arguments)
    return check


def _condition(after, expected):
    def evaluate(step_input):
        return step_input.get_step_content(after) == expected
    return evaluate


def _step_result(step):
    if isinstance(step, list):
        return [_step_result(child) for child in step]
    result = {"name": step.step_name, "success": step.success,
              "content": str(step.content) if step.content is not None else None}
    if step.error:
        result["error"] = step.error
    if step.steps:
        result["steps"] = [_step_result(child) for child in step.steps]
    return result


def _run_batch(tasks, parent_id, settings, prepare):
    if not tasks:
        raise ValueError("tasks 不可为空")
    workers = int(settings.get("delegation.max_parallel"))
    timeout = float(settings.get("delegation.timeout"))
    poll = float(settings.get("delegation.cancel_poll"))
    results = [None] * len(tasks)
    queue = iter(enumerate(tasks))
    active = {}

    def result(index, status, **extra):
        task = tasks[index]
        results[index] = {"index": index, "ref": task.get("ref") if isinstance(task, dict) else None,
                          "status": status, **extra}

    def execute(index, runnable, run_id, reasons):
        if is_cancelled(parent_id):
            result(index, "not_started", reason="主会话已请求取消")
            return
        task = tasks[index]
        output = runnable.run(task["input"], run_id=run_id, session_id=run_id)
        content = output.content
        extra = {"steps": [_step_result(step) for step in output.step_results]} if isinstance(runnable, Workflow) else {}
        result(index, output.status.value.lower(), run_id=run_id,
               content=str(content) if content is not None else None, refusals=list(dict.fromkeys(reasons)), **extra)

    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(tasks)), thread_name_prefix="aaa-delegate") as pool:
            while True:
                cancelled = is_cancelled(parent_id)
                while len(active) < workers and not cancelled:
                    item = next(queue, None)
                    if item is None:
                        break
                    index, task = item
                    try:
                        if (not isinstance(task, dict) or task.keys() != {"ref", "input"}
                                or not isinstance(task["input"], str)):
                            raise ValueError("每项须包含 ref 与文本 input")
                        reasons = []
                        runnable = prepare(task["ref"], reasons)
                        run_id = uuid4().hex
                        future = pool.submit(execute, index, runnable, run_id, reasons)
                        active[future] = (index, runnable, run_id, monotonic() + timeout, None)
                    except Exception as exc:
                        _logger.warning("委派项准备失败（ref=%s）：%s", task.get("ref") if isinstance(task, dict) else "?", type(exc).__name__)
                        result(index, "error", reason=str(exc) or type(exc).__name__)
                    cancelled = is_cancelled(parent_id)
                if cancelled:
                    for index, _ in queue:
                        result(index, "not_started", reason="主会话已请求取消")
                if not active:
                    break
                done, _ = wait(active, timeout=poll, return_when=FIRST_COMPLETED)
                now = monotonic()
                for future, (index, runnable, run_id, deadline, stop) in list(active.items()):
                    if future in done:
                        del active[future]
                        try:
                            future.result()
                        except Exception:
                            _logger.exception("delegation run failed: %s", run_id)
                            result(index, "error", run_id=run_id, reason="子任务运行失败；详见本地日志")
                        if stop:
                            results[index]["stop_requested"] = stop
                    elif stop is None:
                        stop = "parent_cancelled" if is_cancelled(parent_id) else "timeout" if now >= deadline else None
                        if stop:
                            runnable.cancel_run(run_id)
                            active[future] = (index, runnable, run_id, deadline, stop)
    except BaseException:
        # Ctrl-C 等中断穿出等待循环时，先对已启动的子任务补发取消，再向上抛。
        for future, (index, runnable, run_id, _deadline, _stop) in active.items():
            try:
                runnable.cancel_run(run_id)
            except Exception:
                _logger.warning("中断补发取消失败：%s", run_id)
            result(index, "cancelled", run_id=run_id, stop_requested="interrupted")
        raise
    return results


def make_delegation_tools(registry: DelegationRegistry, catalog, models, default_model, build_tools, settings):
    def model_ref(spec):
        if "model" not in spec:
            return default_model
        resolved = catalog.resolve(spec["model"], models)
        if not isinstance(resolved, Resolved):
            raise ValueError(f"委派模型不在可用范围或名称不唯一：{spec['model']}")
        return resolved.ref

    names = _functions(build_tools()).keys()

    def check_actor(spec):
        model_ref(spec)
        unknown = set(spec.get("tools", ())) - names
        if unknown:
            raise ValueError(f"委派工具不可用：{', '.join(sorted(unknown))}")

    def prepare(owner, ref, reasons):
        bundle = registry.resolve(owner, ref)
        public = validate(bundle, registry.public, check_actor)
        strategy = settings.get("boundary.command_approval")

        def build(package, name):
            spec = package["definitions"][name]
            kind = spec["kind"]

            def build_ref(ref):
                if ref.startswith("public:"):
                    target = ref.removeprefix("public:")
                    return build(public[target], target)
                return build(package, ref)

            def step(spec):
                shape = spec["kind"]
                label = spec["name"]
                if shape == "parallel":
                    return Parallel(*(step(child) for child in spec["steps"]), name=label)
                if shape == "condition":
                    return Condition(name=label, evaluator=_condition(spec["after"], spec["equals"]),
                                     steps=[step(child) for child in spec["then"]],
                                     else_steps=[step(child) for child in spec.get("else", ())])
                return Step(name=label, **{shape: build_ref(spec["ref"])})

            if kind == "workflow":
                return Workflow(name=name, steps=[step(item) for item in spec["steps"]], telemetry=False)
            selection = model_ref(spec)
            tools = []
            if selected := spec.get("tools"):
                available = _functions(build_tools())
                for tool_name in dict.fromkeys(selected):
                    fn = available[tool_name]
                    if fn.requires_confirmation:
                        fn.requires_confirmation = False
                        fn.tool_hooks = [_rejection(tool_name, strategy, reasons), *(fn.tool_hooks or ())]
                    tools.append(fn)
            options = dict(name=name, model=catalog.build(selection.provider, selection.model_id),
                           tools=tools, instructions=spec["instructions"], telemetry=False)
            if kind == "team":
                members = [build_ref(member) for member in spec["members"]]
                if spec["mode"] == "tasks":
                    # Agno 3.1.12's execute_task sets status without refreshing dependencies.
                    options["instructions"] += (
                        "\nFor dependent tasks: after a member actually completes a prerequisite, "
                        "call the native update_task_status with its task ID and status completed "
                        "before executing the dependent task. Never mark a failed task completed."
                    )
                return Team(mode=TeamMode(spec["mode"]), members=members, **options)
            return Agent(id=name, **options)

        return build(bundle, bundle["entry"])

    def register_delegation(definition: dict, run_context: RunContext) -> str:
        """Register an Agent, Team or Workflow bundle {entry, definitions} in this session. Definitions are keyed by name; use public:name for saved dependencies."""
        try:
            registry.register(_owner(run_context), definition, check_actor)
            return encode({"ref": definition["entry"], "saved": False})
        except (ValueError, OSError, KeyError, TypeError, RecursionError) as exc:
            return encode({"error": str(exc)})

    def list_delegations(run_context: RunContext) -> str:
        """List visible delegation entries and the host's selectable child models and tools."""
        return encode({"entries": registry.discover(_owner(run_context)),
                       "models": [ref.display for ref in models], "tools": sorted(names)})

    def delegate_task(tasks: list[dict], run_context: RunContext) -> str:
        """Synchronously run registered entries. Each task supplies {ref, input}; input is text. Results retain input order, even on partial failure."""
        owner = _owner(run_context)
        try:
            return encode(_run_batch(tasks, parent_run_id.get() or run_context.run_id, settings,
                                     lambda ref, reasons: prepare(owner, ref, reasons)))
        except Exception:
            _logger.exception("delegate_task 批次失败")
            return encode({"error": "委派批次执行失败；详见本地日志"})

    def save_delegation(definition: dict, run_context: RunContext) -> str:
        """Propose saving this exact registered bundle {entry, definitions} after review. One-offs stay temporary; user confirmation is mandatory."""
        approvals = save_approvals.get()
        key = signature(SAVE_TOOL, {"definition": definition})
        if approvals is None or not approvals.get(key) or is_cancelled(parent_run_id.get() or run_context.run_id):
            return encode({"error": "未获得本次定义保存确认"})
        approvals[key] -= 1
        try:
            registry.save(_owner(run_context), definition)
            return encode({"ref": definition["entry"], "saved": True})
        except (ValueError, OSError, KeyError, TypeError, RecursionError) as exc:
            return encode({"error": str(exc)})

    save = Function.from_callable(save_delegation)
    save.requires_confirmation = True
    return [register_delegation, list_delegations, delegate_task, save]
