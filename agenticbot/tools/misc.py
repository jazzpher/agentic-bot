"""Small built-in tools that need no sandbox."""
import datetime

from agent_loop import ToolError as _WorkspaceError, calculate as _calculate
from agenticbot.tools import ToolError


def register(registry):
    @registry.tool('ask_user', 'Ask one necessary question when only the user can supply missing input (a missing file, a choice, a value). '
                   'Always use this instead of asking in reply text. Ends this turn as needs_input, not completed. Do not ask for passwords or codes.',
                   {'type': 'object', 'properties': {'question': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
                    'required': ['question']}, category='misc', parallel_safe=False)
    def ask_user(ctx, question):
        if not question.strip():
            raise ToolError('question must not be empty')
        ctx.extras['_needs_input'] = question.strip()
        return 'Waiting for user input: ' + question.strip()

    @registry.tool('calculate', 'Evaluate arithmetic exactly (+ - * / // % and parentheses). Prefer run_command with python for anything else.',
                   {'type': 'object', 'properties': {'expression': {'type': 'string', 'maxLength': 200}},
                    'required': ['expression']}, category='misc')
    def calculate(ctx, expression):
        try:
            return str(_calculate(expression))
        except _WorkspaceError as exc:
            raise ToolError(str(exc)) from exc

    @registry.tool('current_time', 'Current date and time (UTC and local server time).',
                   {'type': 'object', 'properties': {}, 'required': []}, category='misc')
    def current_time(ctx):
        now = datetime.datetime.now(datetime.timezone.utc)
        return 'UTC: ' + now.strftime('%Y-%m-%d %H:%M:%S') + ' · local: ' + now.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
