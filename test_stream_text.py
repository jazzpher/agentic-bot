"""Streamed text layout: text before and after a tool call must not run together, in any mode."""
import io
import unittest

from agenticbot.cli import Printer


def feed(printer, events):
    for ev in events:
        printer(ev)


class QuietStreamTests(unittest.TestCase):
    def test_text_around_tool_call_is_not_glued(self):
        out = io.StringIO()
        feed(Printer(verbose=False, stream=out), [
            {'type': 'text', 'delta': 'it.'},
            {'type': 'tool_start', 'name': 'exec', 'arguments': {'command': 'ls'}},
            {'type': 'tool_end', 'name': 'exec', 'output': 'x', 'ok': True},
            {'type': 'text', 'delta': 'The file is there.'},
        ])
        self.assertEqual(out.getvalue(), 'it.\nThe file is there.')

    def test_verbose_still_shows_the_tool_line(self):
        out = io.StringIO()
        feed(Printer(verbose=True, stream=out), [
            {'type': 'text', 'delta': 'it.'},
            {'type': 'tool_start', 'name': 'exec', 'arguments': {'command': 'ls'}},
            {'type': 'tool_end', 'name': 'exec', 'output': 'x', 'ok': True},
            {'type': 'text', 'delta': 'The file is there.'},
        ])
        text = out.getvalue()
        self.assertIn('▸ exec ls', text)
        self.assertIn('it.\n', text)
        self.assertNotIn('it.The', text)


if __name__ == '__main__':
    unittest.main()
