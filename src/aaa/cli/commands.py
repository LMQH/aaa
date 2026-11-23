"""斜杠命令：渠道编辑、模型切换与命令分派。"""

import copy
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

import questionary
from agno.db.base import SessionType
from agno.session import AgentSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_bindings import merge_key_bindings

from ..boundary.policy import MODES
from ..memory import engine_ids
from ..memory.agno import read_builtin
from ..config.loader import CONFIG_VERSION, SECRET_FILE, probe_models, save_config
from ..config.settings import APPLY_RESTART, APPLY_RUNTIME, BY_PATH, save as save_settings
from ..model.catalog import Ambiguous, NotFound, Resolved
from ..model.provider import API_CLASSES, DEFAULT_API
from ..config.paths import DB_FILE
from ..plugins import project_dirs, toggle_extension
from .. import trust as trust_mod
from ..todo_contract import normalize
from ..sessions import index
from ..sessions.query import find_session, list_sessions, session_label
from ..usage import ledger as usage_ledger
from ..usage import report as usage_report
from .. import insight as insight_mod
from .render import sanitize
from .state import AppState
from .ui import GLYPH, UI_STYLE, display_width, pad, print_help

# 渠道 id：用作 `/model <id>/<model>` 的前缀，只允许小写字母、数字与连字符。
CHANNEL_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
NEW_CHANNEL = "＋ 新增渠道"

API_LABELS = {
    "deepseek": "DeepSeek 原生（发 thinking 参数）",
    "openai-completions": "OpenAI 兼容（通用端点）",
}


def _ask_menu(question):
    """在独立循环运行空闲菜单；Esc、Ctrl-C 与 EOF 均取消，不退出 REPL。"""
    app = question.application
    escape = KeyBindings()

    @escape.add("escape", eager=True)
    def _cancel(event) -> None:
        event.app.exit(result=None)

    app.key_bindings = (
        merge_key_bindings([app.key_bindings, escape])
        if app.key_bindings is not None
        else escape
    )
    try:
        return app.run(in_thread=True)
    except (KeyboardInterrupt, EOFError):
        return None


def configure(existing: Optional[Dict], settings) -> Optional[Dict]:
    """渠道编辑器：选择或新增一个渠道，改完写回 secret.yaml。

    返回新配置；用户中途取消时返回 None，不改动已有文件。
    """
    base = copy.deepcopy(existing) if existing else {}
    providers: Dict[str, Dict] = base.get("providers") or {}
    print("\n渠道配置\n")

    provider_id = _choose_channel(providers)
    if provider_id is None:
        return None
    raw = providers.get(provider_id) or {}
    if provider_id == NEW_CHANNEL:
        provider_id = _ask_channel_id(providers)
        if not provider_id:
            return None
        raw = {}

    channel = _edit_channel(provider_id, raw)
    if channel is None:
        return None
    providers[provider_id] = channel

    default = _default_choice(providers, base.get("default"))
    if default is None:
        return None

    config = {
        "version": CONFIG_VERSION,
        "default": default,
        "enabled_models": [item for item in (base.get("enabled_models") or []) if isinstance(item, str)],
        "providers": providers,
    }
    save_config(config)
    print(f"已保存 {SECRET_FILE}")
    return config


def _choose_channel(providers: Dict[str, Dict]) -> Optional[str]:
    if not providers:
        return NEW_CHANNEL
    choices = [
        questionary.Choice(f"{pid}  ({raw.get('base_url')})", value=pid)
        for pid, raw in providers.items()
    ]
    choices.append(questionary.Choice(NEW_CHANNEL, value=NEW_CHANNEL))
    return _ask_menu(questionary.select("编辑哪个渠道", choices=choices, style=UI_STYLE))


def _ask_channel_id(providers: Dict[str, Dict]) -> Optional[str]:
    value = _ask_menu(questionary.text("渠道 id（用作 /model 的前缀）", style=UI_STYLE))
    value = (value or "").strip().lower()
    if not CHANNEL_ID_PATTERN.fullmatch(value):
        print("渠道 id 只能包含小写字母、数字与连字符。")
        return None
    if value in providers:
        print(f"渠道已存在：{value}")
        return None
    return value


def _edit_channel(provider_id: str, raw: Dict) -> Optional[Dict]:
    api = _ask_menu(questionary.select(
        "协议",
        choices=[
            questionary.Choice(API_LABELS.get(name, name), value=name)
            for name in API_CLASSES
        ],
        default=raw.get("api") or DEFAULT_API,
        style=UI_STYLE,
    ))
    if not api:
        return None

    default_url = raw.get("base_url") or (
        "https://api.deepseek.com" if api == DEFAULT_API else ""
    )
    base_url = _ask_menu(questionary.text("Base URL", default=default_url, style=UI_STYLE))
    if not base_url:
        return None

    old_key = raw.get("api_key") or ""
    hint = "API Key（已保存，留空沿用）" if old_key else "API Key"
    api_key = _ask_menu(questionary.password(hint, style=UI_STYLE))
    if api_key is None:
        return None
    api_key = api_key.strip() or old_key
    if not api_key:
        print("未提供 API Key，配置未保存。")
        return None

    previous = {item["id"]: item for item in raw.get("models") or []}
    discovered: List[str] = []
    detected: Dict[str, int] = {}
    print("正在探查可用模型 ...")
    try:
        discovered, detected = probe_models(base_url, api_key)
    except Exception as exc:
        print(f"探查失败：{exc}")

    if discovered:
        chosen = _ask_menu(questionary.checkbox(
            "启用哪些模型（空格选择，回车确认）",
            choices=[
                questionary.Choice(model, value=model, checked=model in previous)
                for model in discovered
            ],
            style=UI_STYLE,
        ))
    else:
        manual = _ask_menu(questionary.text(
            "未探查到模型，请手动输入（逗号分隔）", style=UI_STYLE
        ))
        chosen = [item.strip() for item in (manual or "").split(",") if item.strip()]
    if not chosen:
        print("未选择模型，配置未保存。")
        return None

    models: List[Dict] = []
    for model in chosen:
        old = previous.get(model) or {}
        default = str(
            old.get("context_window")
            or detected.get(model)
            or settings.get("model.default_context_window")
        )
        answer = _ask_menu(questionary.text(
            f"{model} 的上下文窗口（tokens）",
            default=default,
            style=UI_STYLE,
        ))
        entry = dict(old)
        entry["id"] = model
        entry["context_window"] = (
            int(answer) if answer and answer.isdigit() else int(default)
        )
        models.append(entry)

    return {
        "name": raw.get("name") or provider_id,
        "api": api,
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "models": models,
    }


def _default_choice(providers: Dict[str, Dict], current) -> Optional[Dict]:
    references = [
        f"{pid}/{item['id']}"
        for pid, raw in providers.items()
        for item in raw.get("models") or []
    ]
    if not references:
        return None
    keep = None
    if isinstance(current, dict):
        keep = f"{current.get('provider')}/{current.get('model')}"
    picked = keep if keep in references else None
    if picked is None:
        picked = _ask_menu(questionary.select(
            "默认模型", choices=references, style=UI_STYLE
        ))
    if not picked:
        return None
    provider, model_id = picked.split("/", 1)
    return {"provider": provider, "model": model_id}


def handle_command(state: AppState, text: str) -> bool:
    """处理斜杠命令；返回 True 表示已处理，False 表示这行交给模型。"""
    if text == "/insight":
        _insight_command(state)
        return True
    if text == "/trust" or text.startswith("/trust "):
        _trust_command(state, text[len("/trust"):].strip())
        return True
    if text == "/help":
        print_help()
        return True

    if text == "/session":
        action = _ask_menu(questionary.select(
            "会话操作",
            choices=[
                questionary.Choice("切换会话…", value="switch"),
                questionary.Choice("搜索会话…", value="search"),
                questionary.Choice("重命名当前会话…", value="rename"),
                questionary.Choice("删除会话…", value="delete"),
            ],
            style=UI_STYLE,
        ))
        if not action:
            return True
        if action == "switch":
            sessions = list_sessions(state.runnable, limit=int(state.settings.get("sessions.list_limit")))
            if not sessions:
                print("暂无会话。")
                return True
            picked = _ask_menu(questionary.select(
                "选择会话",
                choices=[questionary.Choice(session_label(s), value=s) for s in sessions],
                style=UI_STYLE,
            ))
            if picked:
                _open_session(state, picked)
            return True
        if action == "search":
            keyword = _ask_menu(questionary.text("搜索关键字（留空取消）", style=UI_STYLE))
            if keyword is None or not keyword.strip():
                return True
            return handle_command(state, f"/session search {keyword.strip()}")
        if action == "rename":
            title = _ask_menu(questionary.text("新标题（留空取消）", style=UI_STYLE))
            if title is None or not title.strip():
                return True
            return handle_command(state, f"/session rename {title.strip()}")
        if action == "delete":
            sessions = list_sessions(state.runnable, limit=int(state.settings.get("sessions.list_limit")))
            targets = [s for s in sessions if s.session_id != state.session_id]
            if not targets:
                print("没有可删除的会话（当前会话不能删除；先 /session 切走或 /new）。")
                return True
            picked = _ask_menu(questionary.select(
                "选择要删除的会话",
                choices=[questionary.Choice(session_label(s), value=s) for s in targets],
                style=UI_STYLE,
            ))
            if picked:
                return handle_command(state, f"/session delete {picked.session_id}")
            return True

    if text.startswith("/session "):
        argument = text[len("/session "):].strip()
        parts = argument.split(None, 1)
        sub = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "rename":
            if not rest:
                print("用法：/session rename <新标题>")
                return True
            state.runnable.db.rename_session(state.session_id, SessionType.AGENT, rest)
            print(f"{GLYPH['ok']} 已改名：{rest}")
            return True

        if sub == "delete":
            target = find_session(state.runnable, rest, state.settings)
            if target is None:
                print(f"会话不存在：{rest}")
                return True
            if target.session_id == state.session_id:
                print("当前会话不能删除；先 /session 切走或 /new。")
                return True
            if not _ask_menu(questionary.confirm(
                f"删除会话 {session_label(target)}？此操作不可恢复。", default=False, style=UI_STYLE
            )):
                return True
            state.runnable.db.delete_session(session_id=target.session_id)
            index.forget(DB_FILE, str(Path.cwd()), target.session_id)
            print(f"{GLYPH['ok']} 已删除 {target.session_id}")
            return True

        if sub == "search":
            if not rest:
                print("用法：/session search <关键字>")
                return True
            keyword = rest.lower()
            matches = [
                s for s in list_sessions(state.runnable, limit=int(state.settings.get("sessions.search_limit")))
                if keyword in (s.session_data or {}).get("session_name", "").lower()
                or keyword in s.session_id.lower()
            ]
            if not matches:
                print("没有匹配的会话。")
                return True
            picked = _ask_menu(questionary.select(
                "选择会话",
                choices=[questionary.Choice(session_label(s), value=s) for s in matches],
                style=UI_STYLE,
            ))
            if picked:
                _open_session(state, picked)
            return True

        # 默认：按 id 或前缀打开对话。
        target = find_session(state.runnable, argument, state.settings)
        if target is None:
            print(f"会话不存在：{argument}")
            return True
        _open_session(state, target)
        return True

    if text == "/new":
        state.select_session(None)
        print(f"{GLYPH['ok']} New session started")
        return True

    if text == "/provide":
        updated = configure(state.config, state.settings)
        if updated is not None:
            state.apply_config(updated)
            print(f"{GLYPH['ok']} Model: {state.model_ref.display}")
        return True

    if text == "/usage" or text.startswith("/usage "):
        _usage_command(state, text[len("/usage"):].strip())
        return True

    if text == "/todos":
        _todos_command(state)
        return True

    if text == "/plugin" or text.startswith("/plugin "):
        _plugin_command(state, text[len("/plugin"):].strip())
        return True

    if text == "/skills":
        _skills_command(state)
        return True

    if text == "/export" or text.startswith("/export "):
        _export_command(state, text[len("/export"):].strip())
        return True

    if text == "/compact":
        try:
            result = state.runnable.compact(session_id=state.session_id)
        except Exception as exc:
            print(f"{GLYPH['fail']} {exc}")
            return True
        mark = GLYPH["ok"] if getattr(result, "compacted", False) else GLYPH["dot"]
        print(f"{mark} {getattr(result, 'message', result)}")
        if getattr(result, "compacted", False):
            state.context_tokens = None
        return True

    if text == "/config" or text.startswith("/config "):
        argument = text[len("/config"):].strip()
        _config_command(state, argument)
        return True

    if text == "/mode" or text.startswith("/mode "):
        argument = text[len("/mode"):].strip()
        if not argument:
            argument = _ask_menu(questionary.select(
                "权限模式", choices=MODES, default=state.mode, style=UI_STYLE
            ))
            if not argument:
                return True
        if argument not in MODES:
            print(f"未知模式：{argument}")
            return True
        try:
            state.set_mode(argument)
        except ValueError as exc:
            print(f"{GLYPH['fail']} {exc}")
            return True
        print(f"{GLYPH['ok']} Mode: {state.mode}")
        return True

    if text == "/model" or text.startswith("/model "):
        argument = text[len("/model"):].strip()
        scope = state.scope()
        if not scope:
            print("没有可切换的模型：检查 secret.yaml 的 providers 与 enabled_models。")
            return True

        if argument in ("next", "prev"):
            target = state.cycle_model(1 if argument == "next" else -1)
            if target is None:
                print("可切换的模型不足两个。")
                return True
            state.set_model(target.provider, target.model_id)
            print(f"{GLYPH['ok']} Model: {target.display}")
            return True

        if not argument:
            current = state.model_ref.display
            picked = _ask_menu(questionary.select(
                "选择模型",
                choices=[ref.display for ref in scope],
                default=current if state.model_ref in scope else None,
                style=UI_STYLE,
            ))
            if not picked:
                return True
            argument = picked

        result = state.catalog.resolve(argument, scope)
        if isinstance(result, Ambiguous):
            print(f"多个渠道都有 {argument}，请加渠道前缀：")
            for ref in result.candidates:
                print(f"  {ref.display}")
            return True
        if isinstance(result, NotFound):
            if isinstance(state.catalog.resolve(argument), Resolved):
                print(f"{argument} 不在可切换范围：检查 enabled_models。")
            else:
                print(f"没有匹配的可切换模型：{argument}")
            return True
        state.set_model(result.ref.provider, result.ref.model_id)
        print(f"{GLYPH['ok']} Model: {result.ref.display}")
        return True

    if text.split(None, 1)[:1] == ["/memory"]:
        _memory_command(state, text[len("/memory"):].strip())
        return True

    return False

def _format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _show_setting(state: AppState, path: str) -> None:
    key = BY_PATH.get(path)
    if key is None:
        print(f"未知配置键：{path}")
        return
    print(f"{path} = {_format_value(state.settings.get(path))}")
    print(f"  {state.settings.describe(path)}")


def _apply_setting(state: AppState, path: str) -> None:
    """落盘并按生效方式处理：runtime 重建 Agent，restart 提示下次启动。"""
    key = BY_PATH[path]
    if key.apply == APPLY_RUNTIME:
        state.apply_settings()
        print(f"{GLYPH['ok']} {path} = {_format_value(state.settings.get(path))}")
    else:
        save_settings(state.settings)
        print(f"{GLYPH['ok']} {path} = {_format_value(state.settings.get(path))}")
        if key.apply == APPLY_RESTART:
            print("  该配置在下次启动时生效。")


def _set_setting(state: AppState, path: str, raw: str) -> None:
    if path == "memory.source":
        print("记忆引擎请通过 /memory（或 /memory engine <引擎>）修改。")
        return
    try:
        state.settings.set(path, raw)
    except ValueError as exc:
        print(f"{GLYPH['fail']} {exc}")
        return
    _apply_setting(state, path)


def _config_menu(state: AppState) -> None:
    group = _ask_menu(questionary.select(
        "配置分组", choices=state.settings.groups(), style=UI_STYLE
    ))
    if not group:
        return
    keys = state.settings.keys_of(group)
    choices = [
        questionary.Choice(
            f"{key.short} = {_format_value(state.settings.get(key.path))}", value=key.path
        )
        for key in keys
    ]
    path = _ask_menu(questionary.select("修改哪一项", choices=choices, style=UI_STYLE))
    if not path:
        return
    _show_setting(state, path)
    raw = _ask_menu(questionary.text(
        "新值（留空取消）", default=_format_value(state.settings.get(path)), style=UI_STYLE
    ))
    if raw is None or not raw.strip():
        return
    _set_setting(state, path, raw.strip())


def _config_command(state: AppState, argument: str) -> None:
    if not argument:
        _config_menu(state)
        return
    parts = argument.split(None, 1)
    path = "ui.banner_animation" if parts[0] == "start" else parts[0]
    if len(parts) == 1:
        _show_setting(state, path)
        return
    _set_setting(state, path, parts[1].strip())


# ---------------------------------------------------------------- /todos

def _todos_command(state: AppState) -> None:
    """按阶段展示完整任务清单；编号沿用全局编号。"""
    data = state.runnable.get_session_state(session_id=state.session_id)
    todos = normalize((data or {}).get("todos"))
    if not todos:
        print("任务清单为空。")
        return
    done = sum(1 for item in todos if item.get("status") == "completed")
    print(f"TODO {done}/{len(todos)}")
    order: List[str] = []
    grouped: Dict[str, List] = {}
    for index, item in enumerate(todos, 1):
        phase = item.get("phase") or "任务"
        if phase not in grouped:
            order.append(phase)
            grouped[phase] = []
        grouped[phase].append((index, item))
    for phase in order:
        items = grouped[phase]
        count_done = sum(1 for _, item in items if item.get("status") == "completed")
        print(f"  {phase} {count_done}/{len(items)}")
        for index, item in items:
            mark = {"completed": "[x]", "in_progress": "[>]"}.get(item.get("status"), "[ ]")
            print(f"    {mark} {index}. {item.get('text', '')}")


# ---------------------------------------------------------------- /insight


def _insight_command(state: AppState) -> None:
    """上下文分段估算；只读，不改任何状态，不弹菜单。"""
    data = insight_mod.collect(state)
    print()
    label = _fmt_tokens(data.window) if data.window else "未知"
    print(f"上下文洞察 · 窗口 {label}")
    if data.usage:
        print(
            f"最近一轮 usage：输入 {_fmt_tokens(data.usage.get('input', 0))}"
            f" · 输出 {_fmt_tokens(data.usage.get('output', 0))}"
            f" · 缓存读 {_fmt_tokens(data.usage.get('cached', 0))}"
        )
    print()
    print(pad("#", 5) + pad("类目", 12) + pad("≈tokens", 9, right=True)
          + pad("占比", 7, right=True) + " 来源")
    total = 0
    for seg in data.segments:
        if seg.count is None:
            tokens_text, ratio, approx = "见 1", "", ""
        else:
            total += seg.count.tokens
            tokens_text = f"{seg.count.tokens:,}"
            ratio = f"{seg.count.tokens / data.window * 100:.1f}%" if data.window else "—"
            approx = "≈" if seg.count.approximate else ""
        note = f"（{seg.note}）" if seg.note else ""
        print(pad(seg.number, 5) + pad(seg.name, 12) + pad(tokens_text, 9, right=True)
              + pad(ratio, 7, right=True) + f" {seg.source}{note}{approx}")
    ratio_total = f"{total / data.window * 100:.1f}%" if data.window else "—"
    print(pad("", 5) + pad("合计", 12) + pad(f"{total:,}", 9, right=True)
          + pad(ratio_total, 7, right=True))
    print()
    warnings = "；".join(dict.fromkeys(data.warnings))
    suffix = f"；{warnings}" if warnings else ""
    print(f"脚注：估算 ≈，分词器 {data.tokenizer}{suffix}")


# ---------------------------------------------------------------- /trust


def _trust_command(state: AppState, argument: str) -> None:
    """查看或撤销项目级代码扩展的仓库信任；撤销下次启动生效。"""
    settings = state.settings
    project = project_dirs() if bool(settings.get("project.enabled")) else None
    entries = trust_mod.entries()
    parts = argument.split(None, 1)
    sub = parts[0] if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub:
        if sub != "revoke":
            print("用法：/trust 或 /trust revoke <序号|仓库路径>")
            return
        if not rest:
            print("用法：/trust revoke <序号|仓库路径>")
            return
        roots = sorted(entries)
        target = None
        if rest.isdigit():
            index = int(rest)
            if 1 <= index <= len(roots):
                target = roots[index - 1]
        else:
            try:
                wanted = str(Path(rest).expanduser().resolve())
            except OSError:
                wanted = rest
            if wanted in entries:
                target = wanted
            else:
                target = next((root for root in roots if root.endswith(rest)), None)
        if target is None:
            print(f"没有这条信任记录：{sanitize(rest)}")
            return
        if trust_mod.revoke(target):
            print(f"{GLYPH['ok']} 已撤销信任：{sanitize(target)}（重启生效）")
        else:
            print(f"没有这条信任记录：{sanitize(target)}")
        return

    print("项目信任状态：")
    if project is None:
        print("  项目级发现已关闭（/config project.enabled）。")
    else:
        print(f"  仓库：{sanitize(str(project['root']))}")
        trusted = trust_mod.trusted_kinds(project)
        for kind in trust_mod.KINDS:
            if not trust_mod.describe(project, kind):
                status = "无内容"
            elif kind in trusted:
                status = "已信任"
            else:
                status = "未信任（交互启动时会询问）"
            print(f"  {trust_mod.KIND_LABELS[kind]}：{status}")
    print("已信任仓库：")
    if not entries:
        print("  无")
    else:
        for index, (root, kinds) in enumerate(sorted(entries.items()), 1):
            summary = "、".join(
                f"{trust_mod.KIND_LABELS.get(kind, kind)}:{value[:8]}"
                for kind, value in sorted(kinds.items())
            )
            print(f"  {index}. {sanitize(root)}（{summary}）")


# ---------------------------------------------------------------- /memory


def memory_choices(extra_dir: Optional[Path] = None) -> dict[str, str]:
    return {"off": "关闭", "builtin": "内置"} | {
        f"external:{engine_id}": f"外置：{engine_id}" for engine_id in engine_ids(extra_dir)
    }


def _memory_page(state: AppState, *, limit: int, page: int):
    if state.memory == "builtin":
        user_id = str(state.settings.get("memory.user_id"))
        return read_builtin(state.runnable.db, user_id, limit=limit, page=page)
    return state.memory.read(session_id=state.session_id, limit=limit, page=page)


def _memory_command(state: AppState, argument: str) -> None:
    """/memory 入口：状态 + 引擎菜单；`engine` 直达切换。失败在此收口，绝不把命令交给模型。"""
    try:
        if not argument:
            _memory_status(state)
            _memory_action_menu(state)
            return
        parts = argument.split(None, 1)
        sub = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "engine":
            _memory_engine(state, rest)
        elif sub == "config":
            _memory_config(state, rest)
        elif sub == "embedding":
            _memory_embedding(state, rest)
        else:
            print(f"未知子命令：{sanitize(sub)}（可用：engine、config、embedding）")
    except Exception as exc:
        print(f"{GLYPH['fail']} 记忆命令失败：{sanitize(str(exc))}")


def _memory_action_menu(state: AppState) -> None:
    """空闲引导菜单：切换引擎；外置引擎附带配置入口。Esc 取消，静默返回。"""
    choices = [questionary.Choice("切换记忆引擎…", value="engine")]
    if state.memory is not None and state.memory != "builtin" and state.memory.config_specs():
        choices.append(questionary.Choice("引擎配置…", value="config"))
    action = _ask_menu(questionary.select("记忆操作", choices=choices, style=UI_STYLE))
    if not action:
        return
    if action == "engine":
        _memory_engine(state, "")
    elif action == "config":
        _memory_config(state, "")


def _memory_status(state: AppState) -> None:
    if state.memory is None:
        print("记忆已关闭。")
        return
    label = "内置" if state.memory == "builtin" else f"外置：{sanitize(state.memory.engine_id)}"
    print(f"记忆引擎：{label}")
    result = _memory_page(state, limit=1, page=1)
    if result is None:
        print("当前引擎不提供条目查看。")
    else:
        _, total = result
        print(f"共 {total} 条")


def _ask_engine_choice(choices: dict[str, str]):
    return _ask_menu(questionary.select(
        "选择记忆引擎（重启后生效）",
        choices=[questionary.Choice(label, value=value) for value, label in choices.items()],
        style=UI_STYLE,
    ))


def _memory_engine(state: AppState, source: str) -> None:
    choices = memory_choices(trust_mod.trusted_project_dir(state.settings, "memory_engines"))
    if not source:
        source = _ask_engine_choice(choices)
        if source is None:
            print(f"{GLYPH['dot']} 已取消。")
            return
    if source not in choices:
        print(f"未知记忆引擎：{sanitize(source)}。用 /memory engine 选择。")
        return
    previous = state.settings.get("memory.source")
    state.settings.set("memory.source", source)
    try:
        save_settings(state.settings)
    except Exception as exc:
        state.settings.set("memory.source", previous)
        print(f"{GLYPH['fail']} 保存失败：{sanitize(str(exc))}")
        return
    print(f"{GLYPH['ok']} 已保存：{choices[source]}。重启后生效。")
    if source.startswith("external:miniviking") and not _embedding_installed():
        answer = _ask_menu(questionary.confirm(
            "开启完整能力（安装语义检索增强，约 50MB，需要网络，后台执行）？",
            default=True, style=UI_STYLE,
        ))
        if answer:
            _memory_embedding(state, "install")
        else:
            print("已跳过；之后可用 /memory embedding install 开启。")


def _config_mask(spec: dict) -> str:
    current = spec.get("current")
    if spec.get("secret") and current:
        return "******"
    return _format_value(current)


def _config_label(spec: dict) -> str:
    return f"{spec['key']} = {_config_mask(spec)}  ·  {spec.get('description', '')}"


def _show_config_spec(spec: dict) -> None:
    print(f"{spec['key']}  ·  {spec.get('description', '')}")
    kind = spec.get("type", "str")
    bounds = []
    if spec.get("minimum") is not None:
        bounds.append(f"≥ {spec['minimum']}")
    if spec.get("maximum") is not None:
        bounds.append(f"≤ {spec['maximum']}")
    if spec.get("choices"):
        bounds.append("可选：" + "、".join(str(c) for c in spec["choices"]))
    detail = f"（{kind}，{'，'.join(bounds)}）" if bounds else f"（{kind}）"
    print(f"  当前：{_config_mask(spec)}　{detail}")


def _convert_config_value(spec: dict, raw: str):
    kind = spec.get("type", "str")
    text = raw.strip()
    if kind == "bool":
        low = text.lower()
        if low in ("true", "1", "是", "开"):
            return True
        if low in ("false", "0", "否", "关"):
            return False
        raise ValueError("需要 true / false")
    if kind == "int":
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError("需要整数") from exc
    elif kind == "float":
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError("需要数字") from exc
    else:
        value = text
    choices = spec.get("choices")
    if choices and str(value) not in {str(c) for c in choices}:
        raise ValueError("可选值：" + "、".join(str(c) for c in choices))
    minimum, maximum = spec.get("minimum"), spec.get("maximum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if minimum is not None and value < minimum:
            raise ValueError(f"不能小于 {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"不能大于 {maximum}")
    return value


def _apply_config_value(state: AppState, spec: dict, raw: str) -> None:
    try:
        value = _convert_config_value(spec, raw)
    except (ValueError, TypeError) as exc:
        print(f"{GLYPH['fail']} {sanitize(str(exc))}")
        return
    result = state.memory.apply_config({spec["key"]: value})
    if result:
        print(f"{GLYPH['fail']} {sanitize(result)}")
        return
    shown = "******" if spec.get("secret") and value else sanitize(str(value))
    print(f"{GLYPH['ok']} 已保存：{spec['key']} = {shown}。重启 aaa 后生效。")


def _memory_config(state: AppState, rest: str) -> None:
    """当前记忆引擎的配置面：无参进两级菜单，`config <键> <值>` 直达修改。"""
    if state.memory is None:
        print("记忆已关闭；先切换到一个记忆引擎。")
        return
    if state.memory == "builtin":
        print("内置记忆没有引擎级配置。")
        return
    specs = state.memory.config_specs()
    if not specs:
        print("当前记忆引擎未提供配置面。")
        return
    by_key = {spec["key"]: spec for spec in specs}
    if rest:
        parts = rest.split(None, 1)
        spec = by_key.get(parts[0])
        if spec is None:
            print(f"未知配置键：{sanitize(parts[0])}。用 /memory config 查看。")
            return
        if len(parts) < 2 or not parts[1].strip():
            _show_config_spec(spec)
            return
        _apply_config_value(state, spec, parts[1].strip())
        return
    sections = sorted({spec.get("section") or spec["key"].split(".", 1)[0] for spec in specs})
    section = _ask_menu(questionary.select("配置分组", choices=sections, style=UI_STYLE))
    if not section:
        return
    group = [spec for spec in specs if (spec.get("section") or spec["key"].split(".", 1)[0]) == section]
    key = _ask_menu(questionary.select(
        "修改哪一项",
        choices=[questionary.Choice(_config_label(spec), value=spec["key"]) for spec in group],
        style=UI_STYLE,
    ))
    if not key:
        return
    spec = by_key[key]
    _show_config_spec(spec)
    default = "" if (spec.get("secret") and spec.get("current")) else _format_value(spec.get("current"))
    raw = _ask_menu(questionary.text("新值（留空取消）", default=default, style=UI_STYLE))
    if raw is None or not raw.strip():
        return
    _apply_config_value(state, spec, raw.strip())


def show_memory_status(state: AppState) -> None:
    """运行中放行的 `/memory`：只显示当前引擎状态，不弹菜单。"""
    _memory_status(state)


# ---- /memory embedding：可选增强的安装与状态 ------------------------

_EMBEDDING_INSTALL: dict = {
    "thread": None,
    "running": False,
    "lines": [],
    "ok": None,
}
_INSTALL_LINES_MAX = 60


def _embedding_install_log(line: str) -> None:
    lines = _EMBEDDING_INSTALL["lines"]
    lines.append(line)
    del lines[:-_INSTALL_LINES_MAX]


def _embedding_installed() -> bool:
    try:
        from miniviking.embed_install import extra_installed
    except ImportError:
        return False
    return extra_installed()


def _memory_embedding(state: AppState, argument: str) -> None:
    if argument not in ("", "install", "status"):
        print("用法：/memory embedding [install]")
        return
    installed = _embedding_installed()
    if argument == "install":
        if installed:
            print(f"{GLYPH['ok']} 嵌入增强已安装。重启 aaa 后生效。")
            return
        if _EMBEDDING_INSTALL["running"]:
            print(f"{GLYPH['dot']} 安装已在后台进行，用 /memory embedding 查看进度。")
            return
        _EMBEDDING_INSTALL["lines"].clear()
        _EMBEDDING_INSTALL["ok"] = None
        try:
            from miniviking.embed_install import install
        except ImportError:
            print(f"{GLYPH['fail']} 未找到 miniviking 包，无法安装增强。")
            return

        def job():
            ok = install(_embedding_install_log)
            _EMBEDDING_INSTALL["ok"] = bool(ok)
            _EMBEDDING_INSTALL["running"] = False

        thread = threading.Thread(target=job, name="miniviking-embed-install", daemon=True)
        _EMBEDDING_INSTALL["thread"] = thread
        _EMBEDDING_INSTALL["running"] = True
        thread.start()
        print(f"{GLYPH['dot']} 已开始后台安装（约 50MB，需要网络）。")
        print("继续聊天不受影响；用 /memory embedding 查看进度。")
        print("完成后重启 aaa 生效，模型权重将在下次启动自动下载。")
        return
    # status
    if installed:
        print(f"{GLYPH['ok']} 嵌入增强：已安装。")
        if state.memory is not None and getattr(state.memory, "engine_id", "") == "miniviking":
            print("重启 aaa 后语义检索将自动启用（模型权重在下次启动自动下载）。")
        return
    if _EMBEDDING_INSTALL["running"]:
        thread = _EMBEDDING_INSTALL["thread"]
        alive = bool(thread and thread.is_alive())
        print(f"{GLYPH['dot']} 安装进行中（{'执行中' if alive else '收尾中'}）。")
    elif _EMBEDDING_INSTALL["ok"] is False:
        print(f"{GLYPH['fail']} 上次安装失败，可重试 /memory embedding install。")
    else:
        print("嵌入增强：未安装。用 /memory embedding install 开启完整能力（约 50MB，需要网络）。")
    lines = _EMBEDDING_INSTALL["lines"]
    if lines:
        print("最近日志：")
        for line in lines[-8:]:
            print(f"  {sanitize(line)}")


# ---------------------------------------------------------------- /export

def _open_session(state: AppState, session: AgentSession) -> None:
    state.select_session(session)
    print(f"{GLYPH['ok']} Switched to session {state.session_id}")


def _export_command(state: AppState, argument: str) -> None:
    """把当前会话导出为 markdown transcript；默认写到 cwd 的 session-<id>.md。"""
    if not argument:
        default = str(Path.cwd() / f"session-{state.session_id}.md")
        raw = _ask_menu(questionary.text(
            "导出路径（回车确认，留空或 Esc 取消）", default=default, style=UI_STYLE,
        ))
        if raw is None or not raw.strip():
            print(f"{GLYPH['dot']} 已取消。")
            return
        argument = raw.strip()
    target = Path(argument).expanduser() if argument else Path.cwd() / f"session-{state.session_id}.md"
    runs = state.runnable.db.get_runs(
        session_id=state.session_id, sort_by="created_at", sort_order="asc",
    ) or []
    lines: List[str] = [f"# 会话 {state.session_id}", ""]
    for run in runs:
        messages = getattr(run, "messages", None) or []
        user = next(
            (str(m.content) for m in messages
             if getattr(m, "role", "") == "user" and m.content), None
        )
        if user:
            lines.append("## user")
            lines.append(user.strip())
            lines.append("")
        content = (getattr(run, "content", "") or "").strip()
        if content:
            lines.append("## assistant")
            lines.append(content)
            lines.append("")
        for tool in getattr(run, "tools", None) or []:
            name = getattr(tool, "tool_name", None)
            if name:
                result = getattr(tool, "result", None)
                lines.append(f"- `{name}` → {str(result)[:120] if result else '(无结果)'}")
        lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    print(f"{GLYPH['ok']} 已导出 {len(runs)} 轮 → {target}")


# ---------------------------------------------------------------- /plugin 与 /skills

def _plugin_command(state: AppState, argument: str) -> None:
    """查看或启停 MCP 插件；启停写 `.disabled` 标记文件，重启生效。"""
    exts = state.plugin_extensions
    if argument:
        target = next((e for e in exts if e.name == argument), None)
        if target is None:
            print(f"插件不存在：{argument}（目录 ~/.aaa/extensions/<name>/）")
            return
        enabled = toggle_extension(target.path)
        print(f"{GLYPH['ok']} 插件 {target.name} 已{'启用' if enabled else '停用'}（重启生效）")
        return
    if not exts:
        print("没有发现插件。把插件放到 ~/.aaa/extensions/<name>/extension.toml（[mcp] 段）。")
        return
    picked = _ask_menu(questionary.select(
        "选择插件（回车切换启停，重启生效）",
        choices=[
            questionary.Choice(
                f"{'●' if e.enabled else '○'} {e.name} {_source_tag(e)}  ·  mcp", value=e
            ) for e in exts
        ],
        style=UI_STYLE,
    ))
    if picked:
        enabled = toggle_extension(picked.path)
        print(f"{GLYPH['ok']} 插件 {picked.name} 已{'启用' if enabled else '停用'}（重启生效）")


# ---------------------------------------------------------------- /skills

_SOURCE_LABELS = {"project": "项目", "user": "用户", "builtin": "内置"}


def _source_tag(item) -> str:
    source = getattr(item, "source", "user") or "user"
    return f"[{_SOURCE_LABELS.get(source, source)}]"


def _skills_command(state: AppState) -> None:
    """列出发现的技能；模型可用 load_skill 工具读取正文。"""
    if not state.plugin_skills:
        print("没有发现技能。技能放在 ~/.agents/skills/<name>/SKILL.md 或仓库 .aaa/skills/<name>/SKILL.md。")
        return
    print(f"发现 {len(state.plugin_skills)} 个技能：")
    for s in state.plugin_skills:
        desc = s.description.splitlines()[0] if s.description else ""
        print(f"  {s.name} {_source_tag(s)}  ·  {desc}")


# ---------------------------------------------------------------- /usage

PERIOD_LABELS = {"day": "今天", "week": "本周", "month": "本月"}


def _usage_command(state: AppState, argument: str) -> None:
    """同步账本后按周期展示；同步失败只提示，不挡住已有数据。"""
    period = argument or "day"
    if period not in usage_report.PERIODS:
        print(f"未知统计周期：{period}（可用：day / week / month）")
        return
    try:
        usage_ledger.sync(price=state.catalog.cost)
    except Exception as exc:
        print(f"{GLYPH['fail']} 用量同步失败，仅显示已有记录：{exc}")
    _print_usage(usage_report.report(period))


def _print_usage(data) -> None:
    label = PERIOD_LABELS[data.period]
    span = f"{data.start:%m-%d} ~ {data.end:%m-%d}" if data.period != "day" else f"{data.start:%Y-%m-%d}"
    if not data.rows:
        print(f"{label} {span} · 暂无记录")
        return

    show_cache = any(row.cache_read_tokens for row in data.rows)
    headers = ["渠道", "模型", "输入", "输出"] + (["缓存读"] if show_cache else []) + ["合计", "金额"]
    cells = [
        [row.channel or "未知渠道", row.model, _fmt_tokens(row.input_tokens),
         _fmt_tokens(row.output_tokens)]
        + ([_fmt_tokens(row.cache_read_tokens)] if show_cache else [])
        + [_fmt_tokens(row.tokens), _fmt_money(row.cost)]
        for row in data.rows
    ]
    total = ["合计", "", _fmt_tokens(data.input_tokens), _fmt_tokens(data.output_tokens)] \
        + ([_fmt_tokens(data.cache_read_tokens)] if show_cache else []) \
        + [_fmt_tokens(data.tokens), _fmt_money(data.cost)]

    print()
    print(f"{label} {span} · 用量与花费")
    _table(headers, cells + [total], right_from=2)
    if data.unpriced:
        print(f"  {data.unpriced} 条未定价（渠道未知或缺价），金额不含这些记录。")
    print()


def _table(headers, rows, right_from: int = 0) -> None:
    """对齐的纯文本表：前 `right_from` 列左对齐，其余右对齐；按显示宽度补齐。"""
    widths = [
        max(display_width(row[i]) for row in [headers] + rows)
        for i in range(len(headers))
    ]
    rule = "  " + "  ".join("-" * width for width in widths)
    print("  " + "  ".join(
        pad(cell, widths[i], right=i >= right_from)
        for i, cell in enumerate(headers)
    ))
    print(rule)
    for row in rows:
        print("  " + "  ".join(
            pad(cell, widths[i], right=i >= right_from) for i, cell in enumerate(row)
        ))
    print(rule)


def _fmt_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _fmt_money(value):
    if value is None:
        return "?"
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.01:
        return f"${value:.3f}"
    return f"${value:.4f}"
