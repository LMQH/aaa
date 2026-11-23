"""Immutable delegation bundles and session-local discovery."""

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_KINDS = {"agent", "team", "workflow"}
_MODES = {"coordinate", "route", "broadcast", "tasks"}


def identifier(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"无效委派名称：{value!r}")
    return value


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError(f"字段不匹配：必需 {', '.join(required)}；可选 {', '.join(optional)}")


def _array(value):
    if not isinstance(value, list) or not value:
        raise ValueError("预期非空列表")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("预期非空文本")


def _actor(spec, check_actor):
    _text(spec["instructions"])
    if "model" in spec:
        _text(spec["model"])
    tools = spec.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(name, str) for name in tools):
        raise ValueError("tools 必须为工具名称列表")
    check_actor(spec)


def _steps(steps, resolve, seen, previous):
    for step in _array(steps):
        if not isinstance(step, dict):
            raise ValueError("步骤必须为对象")
        kind = step.get("kind")
        name = identifier(step.get("name"))
        if name in seen:
            raise ValueError(f"步骤重名：{name}")
        seen.add(name)
        if isinstance(kind, str) and kind in _KINDS:
            _object(step, ("kind", "name", "ref"))
            resolve(step["ref"], {kind})
        elif kind == "parallel":
            _object(step, ("kind", "name", "steps"))
            for branch in _array(step["steps"]):
                _steps([branch], resolve, seen, previous.copy())
        elif kind == "condition":
            _object(step, ("kind", "name", "after", "equals", "then"), ("else",))
            if identifier(step["after"]) not in previous or not isinstance(step["equals"], str):
                raise ValueError("条件须比较前序步骤输出与一个字符串")
            _steps(step["then"], resolve, seen, previous.copy())
            if "else" in step:
                _steps(step["else"], resolve, seen, previous.copy())
        else:
            raise ValueError(f"不支持的步骤类型：{kind}")
        previous.add(name)


def validate(bundle, load_public, check_actor):
    """Resolve a complete graph before publishing or starting any native run."""
    visited = set()
    active = set()
    bundles = {}

    def visit(package, name, kinds):
        root = package["entry"]
        definitions = package["definitions"]
        spec = definitions[name]
        kind = spec.get("kind") if isinstance(spec, dict) else None
        if not isinstance(kind, str) or kind not in kinds:
            raise ValueError(f"执行对象类型不匹配：{name}")
        key = (root, name)
        if key in active:
            raise ValueError(f"循环引用：{name}")
        if key in visited:
            return
        active.add(key)

        def resolve(ref, allowed):
            if not isinstance(ref, str):
                raise ValueError("引用必须为文本")
            if ref.startswith("public:"):
                public_name = identifier(ref.removeprefix("public:"))
                public = bundles.get(public_name)
                if public is None:
                    public = load_public(public_name)
                    bundles[public_name] = public
                visit(public, public_name, allowed)
            else:
                if identifier(ref) not in definitions:
                    raise ValueError(f"不存在的私有引用：{ref}")
                visit(package, ref, allowed)

        if "description" in spec:
            _text(spec["description"])
        if kind == "agent":
            _object(spec, ("kind", "instructions"), ("description", "model", "tools"))
            _actor(spec, check_actor)
        elif kind == "team":
            _object(spec, ("kind", "instructions", "mode", "members"), ("description", "model", "tools"))
            if not isinstance(spec["mode"], str) or spec["mode"] not in _MODES:
                raise ValueError("不支持的 Team 模式")
            _actor(spec, check_actor)
            members = _array(spec["members"])
            for ref in members:
                resolve(ref, {"agent"})
            if len(members) != len({ref.removeprefix("public:") for ref in members}):
                raise ValueError("Team 成员 ID 冲突")
        else:
            _object(spec, ("kind", "steps"), ("description",))
            _steps(spec["steps"], resolve, set(), set())
        active.remove(key)
        visited.add(key)

    _object(bundle, ("entry", "definitions"))
    entry = identifier(bundle["entry"])
    definitions = bundle["definitions"]
    if not isinstance(definitions, dict) or entry not in definitions:
        raise ValueError("definitions 必须包含入口定义")
    visit(bundle, entry, _KINDS)
    if any((entry, name) not in visited for name in definitions):
        raise ValueError("包内存在入口未引用的定义")
    return bundles


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class DelegationRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.temporary: dict[tuple[str, str, str], str] = {}

    def _target(self, name):
        return self.path / f"{identifier(name)}.json"

    def public(self, name):
        target = self._target(name)
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"没有已保存的委派对象：{name}")
        bundle = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(bundle, dict) or bundle.get("entry") != name:
            raise ValueError(f"已保存定义无效：{name}")
        return bundle

    def register(self, owner, bundle, check_actor):
        validate(bundle, self.public, check_actor)
        key = (*owner, bundle["entry"])
        if key in self.temporary or self._target(bundle["entry"]).exists():
            raise ValueError(f"委派入口重名：{bundle['entry']}")
        self.temporary[key] = encode(bundle)

    def resolve(self, owner, name):
        raw = self.temporary.get((*owner, identifier(name)))
        return json.loads(raw) if raw is not None else self.public(name)

    def discover(self, owner):
        entries = []
        local = {key[-1]: json.loads(raw) for key, raw in self.temporary.items() if key[:-1] == owner}
        for name in sorted(local.keys() | {p.stem for p in self.path.glob("*.json") if _NAME.fullmatch(p.stem)}):
            try:
                bundle = local[name] if name in local else self.public(name)
                spec = bundle["definitions"][name]
                entries.append({"ref": name, "kind": spec["kind"], "description": spec.get("description", ""),
                                "saved": name not in local})
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return entries

    def save(self, owner, snapshot):
        name = identifier(snapshot["entry"])
        raw = self.temporary.get((*owner, name))
        if raw is None or encode(snapshot) != raw:
            raise ValueError("保存定义与当前会话的临时快照不一致")
        target = self._target(name)
        self.path.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=self.path) as directory:
            staging = Path(directory) / "definition.json"
            staging.write_text(raw, encoding="utf-8")
            target.hardlink_to(staging)
        del self.temporary[(*owner, name)]
