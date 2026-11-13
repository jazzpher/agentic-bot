"""run_command: shell execution through the configured sandbox backend + policy."""
from agenticbot import config, sandbox
from agenticbot.approvals import SandboxPolicy
from agenticbot.tools import ToolError


def _policy(ctx):
    return ctx.policy or SandboxPolicy()


def _backend(ctx):
    backend = ctx.extras.get('sandbox_backend')
    if backend is None:
        backend = sandbox.get_backend()
        ctx.extras['sandbox_backend'] = backend
    return backend


def shell_available(ctx):
    return config.resolve_sandbox_backend() != 'off'


def command_approval(ctx, arguments):
    """Reason string when the command crosses the sandbox boundary, else None."""
    command = str(arguments.get('command', ''))
    policy = _policy(ctx)
    refused = sandbox.hardline_reason(command)
    if refused:
        raise ToolError(refused)
    backend = _backend(ctx)
    # Without an isolated backend (no Docker) nothing confines a command, so every command asks first, in every
    # mode, and saved "always allow" rules do not apply. Only the explicit full-access + never policy skips it.
    ctx.extras['strict_approval'] = bool(not backend.isolated and not policy.yolo)
    if policy.yolo:
        return None
    if ctx.extras['strict_approval']:
        why = 'No Docker sandbox, so this command would run directly on your computer; every command needs your approval.'
        if sandbox.escapes_workspace(command, ctx.workspace.root):
            why += ' It also references paths outside the workspace.'
        return why
    destructive = sandbox.destructive_reason(command)
    if destructive:
        return destructive
    if policy.mode == 'read-only':
        return None if sandbox.is_read_only_command(command) else 'Sandbox is read-only; only inspection commands run without approval.'
    if policy.mode == 'workspace-write':
        if not backend.isolated:
            if sandbox.escapes_workspace(command, ctx.workspace.root):
                return 'Command references paths outside the workspace (subprocess backend cannot confine it).'
            if not policy.network and sandbox.uses_network(command):
                return 'Command appears to use the network, which is off in this policy.'
        return None
    return None


def register(registry):
    @registry.tool('run_command',
                   'Run a shell command in the workspace sandbox and return stdout/stderr/exit code. Use it to run code, '
                   'tests, git, package managers and file inspection. Commands run with cwd = workspace root; long or '
                   'interactive commands are killed at the timeout. Prefer non-interactive flags (e.g. -y, --no-input).',
                   {'type': 'object', 'properties': {
                       'command': {'type': 'string', 'maxLength': 8000},
                       'timeout': {'type': 'integer', 'description': 'Seconds (default %d, max %d)' % (config.COMMAND_TIMEOUT, config.COMMAND_TIMEOUT * 5)},
                       'description': {'type': 'string', 'description': 'Short human-readable purpose shown in the UI'}},
                    'required': ['command']}, category='shell', mutates=True,
                   needs_approval=command_approval, available=shell_available)
    def run_command(ctx, command, timeout=None, description=''):
        command = command.strip()
        if not command:
            raise ToolError('command must not be empty.')
        refused = sandbox.hardline_reason(command)
        if refused:
            raise ToolError(refused)
        policy = _policy(ctx)
        backend = _backend(ctx)
        timeout = max(1, min(int(timeout or config.COMMAND_TIMEOUT), config.COMMAND_TIMEOUT * 5))
        ctx.emit({'type': 'command_start', 'command': command, 'description': description, 'backend': backend.name})
        try:
            result = backend.run(command, ctx.workspace.root, timeout, network=policy.network or policy.mode == 'full-access',
                                 read_only=policy.mode == 'read-only', stop=getattr(ctx, 'stop', None))
        except RuntimeError as exc:
            raise ToolError(str(exc)) from exc
        except OSError as exc:
            raise ToolError('Could not start the command: ' + str(exc)) from exc
        if ctx.workspace is not None:
            # Files may have changed under us; do not pretend read-back state is current.
            ctx.workspace.checked.clear()
        output = result.render()
        network_on = policy.network or policy.mode == 'full-access'
        enforced = backend.isolated or sandbox.network_namespace_supported()
        if not network_on and not enforced:
            output += ('\n[note] Network is OFF by policy but this host cannot enforce it for shell commands '
                       '(no Docker, no Linux network namespace). Do not use the network from the shell.')
        net_error = any(w in output.lower() for w in ('could not resolve', 'name resolution', 'network is unreachable',
                                                      'temporary failure in resolution', 'connection refused', 'failed to connect'))
        if result.exit_code != 0 and not network_on and (sandbox.uses_network(command) or net_error):
            output += ('\nNetwork is OFF for shell commands. Do not work around it with another binary or path. '
                       'For public web content use fetch_url, which has its own URL checks and approvals.')
        elif result.exit_code != 0 and sandbox.uses_network(command):
            output += '\nFor public web reads, try fetch_url instead of repeating failed curl/wget commands.'
        if result.exit_code != 0 and ('pip' in command or 'venv' in command):
            output += ('\nInstall dependencies in a workspace virtualenv: python3 -m venv .venv, then '
                       '.venv/bin/python -m pip install ... (Windows: .venv\\Scripts\\python.exe). '
                       'Do not install into the bot runtime.')
        return output
