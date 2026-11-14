"""Typed tool registry.

Adding a tool is one function with a JSON-schema signature:

    @registry.tool('greet', 'Say hello', {'type': 'object', 'properties': {...}, 'required': [...]})
    def greet(ctx, name):
        return 'hello ' + name

The same registry produces native OpenAI ``tools`` definitions and the prompt
block used by the JSON fallback protocol, validates arguments before execution,
labels output as untrusted data, and bounds its size.
"""
import ast
import dataclasses
import difflib
import json
import re
import threading
import time
from typing import Any, Callable, Dict, Optional

from agenticbot import config

TYPE_MAP = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool,
            'array': list, 'object': dict}


_FENCE = re.compile(r'^```[a-zA-Z]*\s*|\s*```$')
_TRAILING_COMMA = re.compile(r',\s*([}\]])')
_PY_LITERALS = ((re.compile(r'\bTrue\b'), 'true'), (re.compile(r'\bFalse\b'), 'false'),
                (re.compile(r'\bNone\b'), 'null'))


def _first_object(text):
    """The first balanced {...} span in text, or None."""
    start = text.find('{')
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find('{', start + 1)
    return None


def repair_arguments(raw):
    """Best-effort arguments for a weak model's malformed call: a dict, or None if nothing usable.

    Small models emit trailing commas, Python literals, single quotes, a fenced block, a
    double-encoded JSON string, or prose around the object. Each is repaired here so the call runs
    instead of failing on syntax the model cannot see.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return {}
    text = _FENCE.sub('', text).strip()
    for candidate in (text, _first_object(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            data = None
        if isinstance(data, str):                      # double-encoded: a JSON string holding the object
            try:
                data = json.loads(data)
            except ValueError:
                data = None
        if isinstance(data, dict):
            return data
        fixed = _TRAILING_COMMA.sub(r'\1', candidate)
        for pattern, replacement in _PY_LITERALS:
            fixed = pattern.sub(replacement, fixed)
        try:
            data = json.loads(fixed)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
        try:                                           # Python dict literal: single quotes, True/None
            data = ast.literal_eval(candidate)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            data = None
        if isinstance(data, dict):
            return data
    return None


# Names weak models reach for, mapped to the tool that does it. Only ever used to suggest: a call is
# never silently run as a different tool than the one the model named.
_TOOL_ALIASES = {
    'bash': 'run_command', 'shell': 'run_command', 'exec': 'run_command', 'execute': 'run_command',
    'terminal': 'run_command', 'cmd': 'run_command', 'command': 'run_command', 'python': 'run_command',
    'ls': 'list_files', 'dir': 'list_files', 'list_dir': 'list_files', 'listdir': 'list_files',
    'cat': 'read_file', 'open': 'read_file', 'read': 'read_file', 'view': 'read_file',
    'write': 'write_file', 'create_file': 'write_file', 'save_file': 'write_file',
    'edit': 'edit_file', 'replace': 'edit_file', 'patch': 'edit_file',
    'search': 'web_search', 'google': 'web_search', 'fetch': 'fetch_url', 'curl': 'fetch_url',
    'find': 'glob', 'grep': 'grep', 'rg': 'grep',
    'remember': 'save_memory', 'recall': 'search_memory', 'memory': 'search_memory',
    'browse': 'browse_open', 'screenshot': 'browse_see', 'click': 'browse_click',
}


def suggest_tool(name, names):
    """Close matches for a tool name a model got wrong, best first."""
    if not isinstance(name, str) or not name:
        return []
    lowered = {n.lower(): n for n in names}
    if name.lower() in lowered:
        return [lowered[name.lower()]]
    alias = _TOOL_ALIASES.get(name.lower().replace('-', '_').replace(' ', '_'))
    if alias and alias in names:
        return [alias]
    return difflib.get_close_matches(name, list(names), n=3, cutoff=0.6)


class ToolError(ValueError):
    """A tool refused or failed; the message is safe to show to the model and user."""


class ApprovalDenied(ToolError):
    """The user denied a tool call that required approval."""


@dataclasses.dataclass
class ToolContext:
    workspace: Any                                  # agent_loop.Workspace (path guards, limits)
    session_id: str = ''
    policy: Any = None                              # agenticbot.approvals.SandboxPolicy
    approvals: Any = None                           # agenticbot.approvals.ApprovalBroker
    stop: threading.Event = dataclasses.field(default_factory=threading.Event)
    emit: Callable[[dict], None] = lambda event: None
    extras: Dict[str, Any] = dataclasses.field(default_factory=dict)
    turn_id: str = ''


@dataclasses.dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., str]
    category: str = 'general'
    mutates: bool = False
    needs_approval: Optional[Callable[[ToolContext, dict], Optional[str]]] = None
    available: Optional[Callable[[ToolContext], bool]] = None
    parallel_safe: bool = False                      # may run concurrently with other calls in the same step

    def openai(self):
        return {'type': 'function', 'function': {
            'name': self.name, 'description': self.description, 'parameters': self.parameters}}


@dataclasses.dataclass
class ToolResult:
    name: str
    arguments: dict
    output: str
    ok: bool
    duration: float
    truncated: bool = False
    meta: Dict[str, Any] = dataclasses.field(default_factory=dict)


def validate_arguments(spec, arguments):
    """Minimal JSON-schema validation: required keys, primitive types, enums, unknown keys."""
    if not isinstance(arguments, dict):
        raise ToolError(spec.name + ': arguments must be a JSON object.')
    props = spec.parameters.get('properties', {}) or {}
    required = spec.parameters.get('required', []) or []
    missing = [k for k in required if k not in arguments or arguments[k] is None]
    if missing:
        raise ToolError(spec.name + ' requires: ' + ', '.join(missing) + '.')
    if spec.parameters.get('additionalProperties') is False:
        unknown = [k for k in arguments if k not in props]
        if unknown:
            raise ToolError(spec.name + ' got unknown arguments: ' + ', '.join(unknown) + '.')
    clean = {}
    for key, value in arguments.items():
        schema = props.get(key)
        if schema is None:
            continue
        expected = schema.get('type')
        if value is None:
            if key in required:
                raise ToolError(spec.name + ': ' + key + ' must not be null.')
            continue
        if isinstance(expected, str) and expected in TYPE_MAP:
            # JSON models often send numbers as strings; coerce the obvious cases.
            if expected == 'integer' and isinstance(value, str) and value.strip().lstrip('-').isdigit():
                value = int(value)
            elif expected == 'number' and isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    pass
            elif expected == 'boolean' and isinstance(value, str) and value.lower() in ('true', 'false'):
                value = value.lower() == 'true'
            if isinstance(value, bool) and expected in ('integer', 'number'):
                raise ToolError(spec.name + ': ' + key + ' must be a ' + expected + '.')
            if not isinstance(value, TYPE_MAP[expected]):
                raise ToolError(spec.name + ': ' + key + ' must be a ' + expected + '.')
        if 'enum' in schema and value not in schema['enum']:
            raise ToolError(spec.name + ': ' + key + ' must be one of ' + ', '.join(map(str, schema['enum'])) + '.')
        if expected == 'string' and 'maxLength' in schema and len(value) > schema['maxLength']:
            raise ToolError(spec.name + ': ' + key + ' is too long.')
        clean[key] = value
    return clean


def bound_output(text, limit=None):
    limit = limit or config.MAX_TOOL_OUTPUT_CHARS
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    return (text[:head] + '\n…[output truncated: %d of %d characters shown]…\n' % (limit, len(text))
            + text[-tail:]), True


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec):
        if spec.name in self._tools:
            raise ValueError('Duplicate tool: ' + spec.name)
        self._tools[spec.name] = spec
        return spec

    def tool(self, name, description, parameters, **kwargs):
        def decorator(fn):
            self.register(ToolSpec(name, description, parameters, fn, **kwargs))
            return fn
        return decorator

    def names(self):
        return list(self._tools)

    def unregister(self, name):
        return self._tools.pop(name, None)

    def names_for(self, ctx):
        return [name for name, spec in self._tools.items() if spec.available is None or spec.available(ctx)]

    def describe(self):
        """Public summary for /capabilities and the CLI."""
        return [{'name': t.name, 'description': t.description.split('\n')[0][:200], 'category': t.category, 'mutates': t.mutates}
                for t in self._tools.values()]

    def get(self, name):
        return self._tools.get(name)

    def __contains__(self, name):
        return name in self._tools

    def __len__(self):
        return len(self._tools)

    def specs(self, ctx=None):
        for spec in self._tools.values():
            if ctx is not None and spec.available is not None and not spec.available(ctx):
                continue
            yield spec

    def subset(self, names):
        sub = ToolRegistry()
        for name in names:
            if name in self._tools:
                sub.register(self._tools[name])
        return sub

    def openai_tools(self, ctx=None):
        return [spec.openai() for spec in self.specs(ctx)]

    def prompt_block(self, ctx=None):
        """Compact tool documentation for providers without native tool calling."""
        lines = []
        for spec in self.specs(ctx):
            props = spec.parameters.get('properties', {}) or {}
            required = set(spec.parameters.get('required', []) or [])
            args = ', '.join(k + ('' if k in required else '?') + ': ' + str(v.get('type', 'any')) for k, v in props.items())
            lines.append('- ' + spec.name + '(' + args + '): ' + spec.description.split('\n')[0])
        return '\n'.join(lines)

    def execute(self, ctx: ToolContext, name, arguments) -> ToolResult:
        started = time.monotonic()
        spec = self._tools.get(name)
        if spec is None:
            close = suggest_tool(name, self._tools)
            hint = (' Did you mean ' + ' or '.join(close) + '? Call it by that exact name.' if close else
                    ' Call one of the listed tools by its exact name.')
            return ToolResult(name, arguments if isinstance(arguments, dict) else {},
                              'Tool error: unknown tool ' + str(name) + '.' + hint +
                              ' Available: ' + ', '.join(self._tools), False,
                              time.monotonic() - started)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                repaired = repair_arguments(arguments)
                if repaired is None:
                    return ToolResult(name, {}, 'Tool error: arguments were not valid JSON. Send a JSON object '
                                                'of the arguments, e.g. {"path": "."}.', False,
                                      time.monotonic() - started)
                arguments = repaired
        from agenticbot import plugins
        result = None
        try:
            clean = validate_arguments(spec, arguments)
            if spec.available is not None and not spec.available(ctx):
                raise ToolError(name + ' is not available in this configuration.')
            clean = plugins.run_before_tool(ctx, name, clean)
            if spec.needs_approval is not None and ctx.approvals is not None:
                reason = spec.needs_approval(ctx, clean)
                if reason:
                    decision = ctx.approvals.request(ctx, name, clean, reason)
                    if decision != 'allow':
                        raise ApprovalDenied('The user denied ' + name + '. Ask how to proceed instead of retrying.')
            output = spec.handler(ctx, **clean)
            if output is None:
                output = ''
            if isinstance(output, ToolResult):
                # Handlers may return a full result to carry meta (e.g. preview images);
                # the output text is still bounded here.
                text, truncated = bound_output(output.output)
                output.output = text
                output.truncated = output.truncated or truncated
                output.duration = time.monotonic() - started
                result = output
            else:
                text, truncated = bound_output(output)
                result = ToolResult(name, clean, text, True, time.monotonic() - started, truncated)
        except ToolError as exc:
            result = ToolResult(name, arguments if isinstance(arguments, dict) else {}, 'Tool error: ' + str(exc), False, time.monotonic() - started)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            result = ToolResult(name, arguments if isinstance(arguments, dict) else {},
                                'Tool error: ' + type(exc).__name__ + ': ' + str(exc)[:500], False, time.monotonic() - started)
        return plugins.run_after_tool(ctx, result)


def default_registry(plugins_too=True):
    """All built-in tools. Availability of shell/web/memory/scheduler tools is decided per context."""
    from agenticbot.tools import browse, delegate, files, memory, misc, preview, schedule, shell, skills, sortcheck, verify, web
    registry = ToolRegistry()
    for module in (files, misc, skills, shell, web, memory, delegate, schedule, preview, browse, verify, sortcheck):
        module.register(registry)
    if plugins_too:
        from agenticbot import plugins
        plugins.load(registry)
    return registry
