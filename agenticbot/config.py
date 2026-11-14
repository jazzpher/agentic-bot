"""Environment-driven limits and defaults. Every value has a safe default.

Legacy defaults (25 files / 32 KB / 256 KB / 12 steps / 24 000 chars) were far
below what a coding or research agent needs. The new defaults are generous but
still bounded; operators can lower or raise them with the AGENTIC_* variables.
"""
from agenticbot.env import env_get
import ctypes
import os
import shutil
import sys


def _int(name, default, minimum=0):
    try:
        value = int(env_get(name, '') or default)
    except ValueError:
        value = default
    return max(minimum, value)


def _choice(name, default, allowed):
    value = (env_get(name, '') or default).strip().lower()
    return value if value in allowed else default


# Workspace limits
MAX_FILE_BYTES = _int('AGENTIC_MAX_FILE_BYTES', 10 * 1024 * 1024, 1024)
MAX_WORKSPACE_BYTES = _int('AGENTIC_MAX_WORKSPACE_BYTES', 500 * 1024 * 1024, 65536)
MAX_FILES = _int('AGENTIC_MAX_FILES', 2000, 1)
MAX_READ_BYTES = _int('AGENTIC_MAX_READ_BYTES', 2 * 1024 * 1024, 1024)

# Agent loop
MAX_STEPS = _int('AGENTIC_MAX_STEPS', 50, 1)
MAX_TOOL_OUTPUT_CHARS = _int('AGENTIC_MAX_TOOL_OUTPUT', 20000, 500)
MAX_PARALLEL_TOOLS = _int('AGENTIC_MAX_PARALLEL_TOOLS', 8, 1)
# Actual calls (not model steps); parallel batches cannot bypass these limits.
MAX_TOOL_CALLS_PER_TURN = _int('AGENTIC_MAX_TOOL_CALLS_PER_TURN', 120, 1)
MAX_SEARCHES_PER_TURN = _int('AGENTIC_MAX_SEARCHES_PER_TURN', 6, 1)
MAX_SUBAGENT_DEPTH = _int('AGENTIC_MAX_SUBAGENT_DEPTH', 1, 0)
# Progress guard (agenticbot/loopguard.py): nudge, then block, a call repeated with nothing changed.
LOOP_GUARD = _choice('AGENTIC_LOOP_GUARD', 'on', {'on', 'off'})
LOOP_GUARD_REPEATS = _int('AGENTIC_LOOP_GUARD_REPEATS', 2, 1)

# MCP (Model Context Protocol) client: config file with {"mcpServers": {...}} (Codex/Claude layout)
MCP_CONFIG = env_get('AGENTIC_MCP_CONFIG', '')            # default: ./mcp.json next to webui.py if present
MCP_CONNECT_TIMEOUT = _int('AGENTIC_MCP_CONNECT_TIMEOUT', 20, 1)
MCP_CALL_TIMEOUT = _int('AGENTIC_MCP_CALL_TIMEOUT', 90, 1)

# Scheduler
SCHEDULER_TICK = _int('AGENTIC_SCHEDULER_TICK', 30, 5)
SCHEDULER_MAX_JOBS = _int('AGENTIC_SCHEDULER_MAX_JOBS', 100, 1)
SCHEDULED_MAX_STEPS = _int('AGENTIC_SCHEDULED_MAX_STEPS', 25, 1)

# Telegram gateway (fail closed: no allowlist, no bot)
TELEGRAM_TOKEN = env_get('AGENTIC_TELEGRAM_TOKEN', '')
TELEGRAM_ALLOWED_IDS = {x.strip() for x in env_get('AGENTIC_TELEGRAM_ALLOWED_IDS', '').split(',') if x.strip()}
DEFAULT_CONTEXT_TOKENS = _int('AGENTIC_CONTEXT_TOKENS', 128000, 4000)
COMPACT_RATIO = 0.70          # summarize when estimated tokens exceed this share of the window
KEEP_RECENT_MESSAGES = 8      # always keep the newest messages verbatim after compaction
TOOL_MODE = _choice('AGENTIC_TOOL_MODE', 'auto', ('auto', 'native', 'json'))
# Chat voice: 'friend' (short, warm, mirrors the user's language) or 'tool' (the original terse coding-agent voice).
# GROK_PERSONA is still read as a legacy alias.
PERSONA = _choice('AGENTIC_PERSONA', 'friend', ('friend', 'tool'))

# Provider stream
# Hard wall-clock cap for ONE streaming provider response. The per-read timeout in
# providers.DEFAULT_TIMEOUT cannot bound a provider that keeps dribbling bytes; this can.
STREAM_TIMEOUT = _int('AGENTIC_STREAM_TIMEOUT', 600, 30)

# Edit review: when on, write_file/edit_file/copy_file/write_docx ask for approval with a
# unified diff of the proposed change before anything is written. Scheduled/unattended runs
# use approval=never, which DENIES these edits - do not combine this with unattended schedules.
APPROVE_EDITS = _choice('AGENTIC_APPROVE_EDITS', 'off', ('on', 'off')) == 'on'

# Scheduled/unattended runs are never stronger than interactive ones: a saved full-access policy is downgraded to
# workspace-write for them, so no host command runs without a human. Set AGENTIC_UNATTENDED_FULL_ACCESS=on only if you
# really want scheduled jobs to run shell commands with no approval at all.
UNATTENDED_FULL_ACCESS = _choice('AGENTIC_UNATTENDED_FULL_ACCESS', 'off', ('on', 'off')) == 'on'

# Sandbox
# AGENTIC_SANDBOX_BACKEND is the documented name; AGENTIC_SANDBOX is accepted as a short alias.
SANDBOX_BACKEND = _choice('AGENTIC_SANDBOX_BACKEND' if env_get('AGENTIC_SANDBOX_BACKEND') else 'AGENTIC_SANDBOX', 'auto',
                          ('auto', 'docker', 'subprocess', 'off'))
SANDBOX_IMAGE = env_get('AGENTIC_SANDBOX_IMAGE', 'python:3.12-slim')
COMMAND_TIMEOUT = _int('AGENTIC_COMMAND_TIMEOUT', 120, 1)
COMMAND_MAX_OUTPUT = _int('AGENTIC_COMMAND_MAX_OUTPUT', 60000, 1000)
SANDBOX_MEMORY = env_get('AGENTIC_SANDBOX_MEMORY', '1g')
SANDBOX_CPUS = env_get('AGENTIC_SANDBOX_CPUS', '1.0')
SANDBOX_NETWORK = _choice('AGENTIC_SANDBOX_NETWORK', 'off', ('off', 'on'))
DEFAULT_SANDBOX_MODE = _choice('AGENTIC_SANDBOX_MODE', 'workspace-write', ('read-only', 'workspace-write', 'full-access'))
DEFAULT_APPROVAL = _choice('AGENTIC_APPROVAL', 'on-request', ('on-request', 'never'))
APPROVAL_TIMEOUT = _int('AGENTIC_APPROVAL_TIMEOUT', 600, 10)

# Optional integrity guard for benchmark/eval runs: with this on, the file tools refuse to modify
# test files, so a run cannot "fix" a failing test by rewriting the test itself.
TESTS_READ_ONLY = _choice('AGENTIC_TESTS_READ_ONLY', 'off', ('on', 'off')) == 'on'

# Web tools
SEARCH_BACKEND = _choice('AGENTIC_SEARCH_BACKEND', 'auto', ('auto', 'tavily', 'brave', 'searxng', 'duckduckgo', 'off'))
TAVILY_API_KEY = env_get('TAVILY_API_KEY', '')
BRAVE_API_KEY = env_get('BRAVE_API_KEY', '')
SEARXNG_URL = env_get('SEARXNG_URL', '')
FETCH_MAX_BYTES = _int('AGENTIC_FETCH_MAX_BYTES', 3 * 1024 * 1024, 65536)
FETCH_MAX_CHARS = _int('AGENTIC_FETCH_MAX_CHARS', 40000, 1000)
FETCH_TIMEOUT = _int('AGENTIC_FETCH_TIMEOUT', 20, 3)


def docker_available():
    return shutil.which('docker') is not None


def resolve_sandbox_backend():
    if SANDBOX_BACKEND == 'auto':
        return 'docker' if docker_available() else 'subprocess'
    return SANDBOX_BACKEND

# Preview tool (optional Playwright dependency; workspace-only, loopback static server)
PREVIEW_ENABLED = _choice('AGENTIC_PREVIEW', 'off', ('on', 'off')) == 'on'
# Attach the screenshot as an image for the model (needs a vision-capable model);
# 'off' keeps the layout numbers only, for text-only models.
PREVIEW_IMAGES = _choice('AGENTIC_PREVIEW_IMAGES', 'on', ('on', 'off')) == 'on'

# Browser tools (optional Playwright dependency)
BROWSER_ENABLED = _choice('AGENTIC_BROWSER_ENABLED', 'off', ('on', 'off')) == 'on'
# click/type can act on pages; the task loop has no approval prompt, so they need their own opt-in.
BROWSER_INTERACT = _choice('AGENTIC_BROWSER_INTERACT', 'off', ('on', 'off')) == 'on'
BROWSER_HEADLESS = _choice('AGENTIC_BROWSER_HEADLESS', 'yes', ('yes', 'no')) == 'yes'
BROWSER_EXECUTABLE = env_get('AGENTIC_BROWSER_EXECUTABLE', '')
BROWSER_ARGS = [a for a in env_get('AGENTIC_BROWSER_ARGS', '').split(',') if a]
BROWSER_TEXT_LIMIT = _int('AGENTIC_BROWSER_TEXT_LIMIT', 12000, 200)
BROWSER_TIMEOUT = _int('AGENTIC_BROWSER_TIMEOUT', 30, 1)

# Interactive web browsing for the /agent composer (optional Playwright dependency).
# Separate from AGENTIC_BROWSER_ENABLED (durable /task loop) and AGENTIC_PREVIEW (workspace HTML):
# this lets the interactive agent open public web pages and SEE them. Off by default.
AGENT_BROWSER_ENABLED = _choice('AGENTIC_AGENT_BROWSER', 'off', ('on', 'off')) == 'on'
# Linux always requires bubblewrap. Windows/macOS must explicitly opt into the
# weaker, Chromium-proxy-enforced transport (not an OS sandbox).
AGENT_BROWSER_ISOLATION = _choice('AGENTIC_AGENT_BROWSER_ISOLATION', 'namespace', ('namespace', 'proxy'))
# Attach screenshots as images for the model (needs a vision-capable model); 'off' keeps the
# text snapshot only, for text-only models.
AGENT_BROWSER_IMAGES = _choice('AGENTIC_AGENT_BROWSER_IMAGES', 'on', ('on', 'off')) == 'on'
# Total browse actions (open/see/scroll/click) allowed per turn.
AGENT_BROWSER_MAX_ACTIONS = _int('AGENTIC_AGENT_BROWSER_MAX_ACTIONS', 8, 1)
# Seconds the agent waits for the user to finish a browser take-over (request_takeover) before it goes on
# with the page as the user left it.
AGENT_BROWSER_TAKEOVER_TIMEOUT = _int('AGENTIC_AGENT_BROWSER_TAKEOVER_TIMEOUT', 600, 30)

# Secrets protection. Every child process (approved shell commands on the subprocess backend, MCP servers, the
# browser) runs as the same uid as this process, and a scrubbed child env does not help: /proc/<ppid>/environ
# still shows the environment this process was started with (AGENTIC_API_KEY, AGENTIC_TELEGRAM_TOKEN, AGENTIC_APP_PASSWORD,
# AGENTIC_SESSION_SECRET, search keys), and changing os.environ later does not rewrite it. Marking the process
# non-dumpable makes the kernel refuse /proc/<pid>/environ, /proc/<pid>/mem and ptrace to same-uid processes that
# lack CAP_SYS_PTRACE. Side effects: no core dumps, and debuggers such as py-spy/gdb need root (or
# AGENTIC_ALLOW_DEBUGGER=on to skip this).
ALLOW_DEBUGGER = _choice('AGENTIC_ALLOW_DEBUGGER', 'off', ('on', 'off')) == 'on'
_PR_SET_DUMPABLE, _PR_GET_DUMPABLE = 4, 3


def protect_process():
    """Make this process's /proc/<pid>/environ unreadable to its children (Linux). True when protected."""
    if ALLOW_DEBUGGER or not sys.platform.startswith('linux'):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
        return libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


PROCESS_PROTECTED = protect_process()

# Bounded room for desktop/mobile repair iterations.
PREVIEW_MAX_ROUNDS = min(20, _int('AGENTIC_PREVIEW_MAX_ROUNDS', 8, 1))
SANDBOX_PIDS = min(4096, _int('AGENTIC_SANDBOX_PIDS', 1024, 64))
