"""Tests for the browser take-over (request_takeover + live view): broker scoping and input validation, the
tool's pause/resume contract with a fake browser session, the owner-scoped web routes, the UI wiring, and a
real-Chromium pass (skips unless AGENTIC_LIVE_BROWSE_TESTS=1, like test_agent_browse.LiveChromiumTests)."""
import html
import http.server
import shutil
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from agenticbot import agent_browser, config
from agenticbot.agent_browser import BrowserError
from agenticbot.takeover import MAX_QUEUE, TakeoverBroker, validate_input
from agenticbot.tools import ToolContext, default_registry
from agenticbot.tools import browse as browse_tool
import test_agent_browse
from test_agent_browse import fake_state, make_workspace
from test_vetproxy import _cap_gate, _LIVE

VIEWPORT = {'width': 1280, 'height': 800}
FAKE_JPEG = b'\xff\xd8\xff\xe0' + b'frame' * 20


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.broker = TakeoverBroker()
        self.t = self.broker.start('alice', 'turn1', 'Solve the captcha', VIEWPORT)

    def test_owner_scoping(self):
        self.assertIs(self.broker.get(self.t.id, 'alice'), self.t)
        self.assertIsNone(self.broker.get(self.t.id, 'mallory'))
        self.assertFalse(self.broker.finish(self.t.id, 'mallory'))
        self.assertIsNone(self.broker.send_input(self.t.id, 'mallory', {'type': 'key', 'key': 'Enter'}))
        self.assertEqual(self.broker.list_active('mallory'), [])
        self.assertEqual([t['id'] for t in self.broker.list_active('alice')], [self.t.id])
        self.assertFalse(self.t.done.is_set())

    def test_finish_and_end(self):
        self.assertTrue(self.broker.finish(self.t.id, 'alice'))
        self.assertTrue(self.t.done.is_set())
        self.broker.end(self.t.id)
        self.assertIsNone(self.broker.get(self.t.id, 'alice'))
        self.assertFalse(self.broker.finish(self.t.id, 'alice'))

    def test_input_queue_and_cap(self):
        self.assertTrue(self.broker.send_input(self.t.id, 'alice', {'type': 'type', 'text': 'hi'}))
        self.assertEqual(self.t.drain(), [{'type': 'type', 'text': 'hi'}])
        self.assertEqual(self.t.drain(), [])
        for _ in range(MAX_QUEUE):
            self.assertTrue(self.broker.send_input(self.t.id, 'alice', {'type': 'key', 'key': 'Tab'}))
        self.assertFalse(self.broker.send_input(self.t.id, 'alice', {'type': 'key', 'key': 'Tab'}))

    def test_frames(self):
        self.assertIsNone(self.t.get_frame())
        self.t.set_frame(b'one')
        self.t.set_frame(b'two')
        self.assertEqual(self.t.get_frame(), (b'two', 2))

    def test_validate_input(self):
        self.assertEqual(validate_input({'type': 'click', 'x': 10, 'y': 20.5}, VIEWPORT),
                         {'type': 'click', 'x': 10.0, 'y': 20.5})
        self.assertEqual(validate_input({'type': 'scroll', 'dy': -600}, VIEWPORT), {'type': 'scroll', 'dy': -600.0})
        self.assertEqual(validate_input({'type': 'key', 'key': 'Backspace'}, VIEWPORT), {'type': 'key', 'key': 'Backspace'})
        bad = [None, [], {}, {'type': 'eval', 'js': '1'}, {'type': 'click', 'x': 2000, 'y': 1},
               {'type': 'click', 'x': -1, 'y': 1}, {'type': 'click', 'x': True, 'y': 1}, {'type': 'click', 'x': '1', 'y': 1},
               {'type': 'type', 'text': ''}, {'type': 'type', 'text': 'x' * 501}, {'type': 'type', 'text': 5},
               {'type': 'key', 'key': 'Control+W'}, {'type': 'key', 'key': 'a'}, {'type': 'scroll', 'dy': 99999},
               {'type': 'scroll', 'dy': False}]
        for event in bad:
            with self.assertRaises(ValueError, msg=repr(event)):
                validate_input(event, VIEWPORT)


class FakeSession:
    """Stands in for AgentBrowserSession: waits like the real worker loop, without Chromium."""

    def __init__(self, page=True, status=None):
        self.page = page
        self.status = status
        self.seen_inputs = []
        self.calls = 0

    def has_page(self):
        return self.page

    def takeover(self, handle, approved_hosts, images=True, stop=None):
        self.calls += 1
        handle.set_frame(FAKE_JPEG)
        deadline = time.monotonic() + 5
        while not handle.done.is_set() and time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                return {'status': 'stopped'}
            self.seen_inputs.extend(handle.drain())
            time.sleep(0.01)
        self.seen_inputs.extend(handle.drain())
        state = fake_state(url='https://example.com/after', images=images)
        state['status'] = self.status or ('done' if handle.done.is_set() else 'timeout')
        return state


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.addCleanup(shutil.rmtree, self.ws.root, True)
        self.events = []
        self.broker = TakeoverBroker()
        self.ctx = ToolContext(workspace=self.ws, session_id='alice', turn_id='turn1', emit=self.events.append)
        self.ctx.extras.update({'takeovers': self.broker, 'run_source': 'agent',
                                'agent_browser_session': FakeSession()})
        browse_tool._TURN_LOCAL.turn = None
        browse_tool._TURN_LOCAL.count = 0
        patcher = mock.patch.object(config, 'AGENT_BROWSER_ENABLED', True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reg = default_registry(plugins_too=False)

    def user(self, *inputs, done=True):
        """Act as the web UI: wait for the take-over event, send input, then tap Done."""
        def run():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                started = [e for e in self.events if e['type'] == 'takeover_required']
                if started:
                    tid = started[0]['id']
                    for event in inputs:
                        self.broker.send_input(tid, 'alice', event)
                    if done:
                        time.sleep(0.05)
                        self.broker.finish(tid, 'alice')
                    return
                time.sleep(0.01)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_listed_with_browse_tools_only_when_enabled(self):
        self.assertIn('request_takeover', self.reg.names_for(self.ctx))
        with mock.patch.object(config, 'AGENT_BROWSER_ENABLED', False):
            self.assertNotIn('request_takeover', self.reg.names_for(self.ctx))

    def test_pauses_until_done_then_returns_fresh_view(self):
        thread = self.user({'type': 'type', 'text': 'hunter2-secret'}, {'type': 'key', 'key': 'Enter'})
        result = self.reg.execute(self.ctx, 'request_takeover', {'reason': 'Log in to the site'})
        thread.join(5)
        self.assertTrue(result.ok, result.output)
        self.assertIn('tapped Done', result.output)
        self.assertIn('URL: https://example.com/after', result.output)
        self.assertTrue(result.meta.get('images'))
        kinds = [e['type'] for e in self.events]
        self.assertEqual(kinds, ['takeover_required', 'takeover'])
        self.assertEqual(self.events[0]['reason'], 'Log in to the site')
        self.assertEqual(self.events[0]['viewport'], agent_browser.VIEWPORT)
        self.assertEqual(self.events[1], {'type': 'takeover', 'id': self.events[0]['id'], 'status': 'done'})
        self.assertEqual(self.broker.list_active('alice'), [])
        # The user's input reached the browser, and never the model or the event stream.
        session = self.ctx.extras['agent_browser_session']
        self.assertIn({'type': 'type', 'text': 'hunter2-secret'}, session.seen_inputs)
        self.assertNotIn('hunter2', result.output)
        self.assertNotIn('hunter2', repr(self.events))

    def test_stop_ends_takeover_as_tool_error(self):
        def stop_soon():
            time.sleep(0.1)
            self.ctx.stop.set()
        threading.Thread(target=stop_soon, daemon=True).start()
        result = self.reg.execute(self.ctx, 'request_takeover', {'reason': 'Solve the captcha'})
        self.assertFalse(result.ok)
        self.assertIn('Stopped', result.output)
        self.assertEqual(self.events[-1]['status'], 'stopped')
        self.assertEqual(self.broker.list_active('alice'), [])

    def test_timeout_reports_page_as_left(self):
        self.ctx.extras['agent_browser_session'] = FakeSession(status='timeout')
        thread = self.user()
        result = self.reg.execute(self.ctx, 'request_takeover', {'reason': 'Pay'})
        thread.join(5)
        self.assertTrue(result.ok, result.output)
        self.assertIn('timed out', result.output)
        self.assertEqual(self.events[-1]['status'], 'timeout')

    def test_refused_without_live_viewer(self):
        for extras in ({'unattended': True}, {'run_source': 'scheduled'}, {'run_source': 'telegram'},
                       {'takeovers': None}):
            self.events.clear()
            saved = dict(self.ctx.extras)
            self.ctx.extras.update(extras)
            result = self.reg.execute(self.ctx, 'request_takeover', {'reason': 'Log in'})
            self.ctx.extras.clear()
            self.ctx.extras.update(saved)
            self.assertFalse(result.ok, extras)
            self.assertIn('web UI', result.output)
            self.assertEqual(self.events, [])
        self.assertEqual(self.ctx.extras['agent_browser_session'].calls, 0)

    def test_needs_an_open_page(self):
        self.ctx.extras['agent_browser_session'] = FakeSession(page=False)
        result = self.reg.execute(self.ctx, 'request_takeover', {'reason': 'Log in'})
        self.assertFalse(result.ok)
        self.assertIn('browse_open', result.output)
        del self.ctx.extras['agent_browser_session']
        result = self.reg.execute(self.ctx, 'request_takeover', {'reason': 'Log in'})
        self.assertFalse(result.ok)
        self.assertIn('browse_open', result.output)

    def test_blank_reason_rejected_and_budget_spent(self):
        self.assertFalse(self.reg.execute(self.ctx, 'request_takeover', {'reason': '   '}).ok)
        with mock.patch.object(config, 'AGENT_BROWSER_MAX_ACTIONS', 1):
            thread = self.user()
            self.assertTrue(self.reg.execute(self.ctx, 'request_takeover', {'reason': 'Log in'}).ok)
            thread.join(5)
            result = self.reg.execute(self.ctx, 'request_takeover', {'reason': 'Log in'})
        self.assertFalse(result.ok)
        self.assertIn('budget', result.output)

    def test_browser_error_still_ends_takeover(self):
        session = mock.Mock()
        session.has_page.return_value = True
        session.takeover.side_effect = BrowserError('The page stopped responding for 20 seconds; the browser was closed.')
        self.ctx.extras['agent_browser_session'] = session
        result = self.reg.execute(self.ctx, 'request_takeover', {'reason': 'Log in'})
        self.assertFalse(result.ok)
        self.assertIn('stopped responding', result.output)
        self.assertEqual(self.events[-1]['status'], 'error')
        self.assertEqual(self.broker.list_active('alice'), [])


class RouteTests(unittest.TestCase):
    def setUp(self):
        import webui
        self.webui = webui
        patcher = mock.patch.object(webui, 'PASSWORD', '')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.alice = webui.app.test_client()
        self.mallory = webui.app.test_client()
        for client, sid in ((self.alice, 'alice-sid'), (self.mallory, 'mallory-sid')):
            with client.session_transaction(base_url='http://localhost') as sess:
                sess['sid'] = sid
        self.t = webui.TAKEOVERS.start('alice-sid', 'turn1', 'Log in', VIEWPORT)
        self.addCleanup(webui.TAKEOVERS.end, self.t.id)
        self.base = '/agent/takeover/' + self.t.id

    def get(self, client, path):
        return client.get(path, base_url='http://localhost')

    def post(self, client, path, **kwargs):
        return client.post(path, base_url='http://localhost', **kwargs)

    def test_frame(self):
        self.assertEqual(self.get(self.alice, self.base + '/frame').status_code, 204)
        self.t.set_frame(FAKE_JPEG)
        response = self.get(self.alice, self.base + '/frame')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'image/jpeg')
        self.assertEqual(response.data, FAKE_JPEG)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.get(self.mallory, self.base + '/frame').status_code, 404)
        self.assertEqual(self.get(self.alice, '/agent/takeover/nope/frame').status_code, 404)

    def test_input(self):
        response = self.post(self.alice, self.base + '/input', json={'type': 'type', 'text': 'jazz'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.t.drain(), [{'type': 'type', 'text': 'jazz'}])
        self.assertEqual(self.post(self.alice, self.base + '/input', json={'type': 'eval'}).status_code, 400)
        self.assertEqual(self.post(self.alice, self.base + '/input', data='x' * 9000,
                                   content_type='application/json').status_code, 413)
        self.assertEqual(self.post(self.mallory, self.base + '/input', json={'type': 'key', 'key': 'Enter'}).status_code, 404)
        self.assertEqual(self.t.drain(), [])
        cross = self.post(self.alice, self.base + '/input', json={'type': 'key', 'key': 'Enter'},
                          headers={'Origin': 'https://evil.example'})
        self.assertEqual(cross.status_code, 403)
        self.assertEqual(self.t.drain(), [])

    def test_done(self):
        self.assertEqual(self.post(self.mallory, self.base + '/done').status_code, 404)
        self.assertFalse(self.t.done.is_set())
        self.assertEqual(self.post(self.alice, self.base + '/done').status_code, 200)
        self.assertTrue(self.t.done.is_set())

    def test_turns_get_the_broker(self):
        captured = {}

        def fake_run_turn(messages, registry, ctx, cfg, emit, **kwargs):
            captured.update(ctx.extras)
            from agenticbot.agent import TurnResult
            return TurnResult(status='completed', text='ok', messages=list(messages))
        store = mock.Mock()
        store.get_context.return_value = ([], None)
        with mock.patch.object(self.webui, 'conversation_store', return_value=store), \
                mock.patch.object(self.webui.agent_core, 'run_turn', side_effect=fake_run_turn), \
                mock.patch.object(self.webui, 'mcp_manager'), mock.patch.object(self.webui, 'memory_store'), \
                mock.patch.object(self.webui, 'scheduler'), mock.patch.object(self.webui, 'config_snapshot',
                                                                            return_value=('', '', 'm')):
            self.webui.run_owner_turn('alice-sid', make_workspace(), 'cid', 'hi', emit=lambda e: None,
                                      stop=threading.Event())
        self.assertIs(captured['takeovers'], self.webui.TAKEOVERS)
        self.assertEqual(captured['run_source'], 'agent')


class UiWiringTests(unittest.TestCase):
    def test_app_js_handles_takeover_events(self):
        with open('agenticbot/static/app.js', encoding='utf-8') as fh:
            code = fh.read()
        for needle in ("case 'takeover_required'", "case 'takeover'", "ev.type === 'takeover_required' && !ev.resolved",
                       "'/frame?t='", "'/input'", "'/done'", "field.type = 'password'"):
            self.assertIn(needle, code)


# ---------------- live Chromium ----------------

PAGE_LOGIN = ('<html><head><title>Login</title></head><body style="margin:0">'
              '<form action="/welcome" method="get">'
              '<input name="user" style="position:absolute;left:100px;top:100px;width:300px;height:40px">'
              '</form>'
              '<a href="http://site-b.test:{port}/other" '
              'style="position:absolute;left:100px;top:300px;width:300px;height:40px;display:block">Elsewhere</a>'
              '</body></html>')


class _FakeHandle:
    """Minimal take-over handle for the session loop: no broker, no frames."""

    def __init__(self):
        self.done = threading.Event()

    def drain(self):
        return []

    def set_frame(self, frame):
        pass


class _FakePage:
    """Playwright page double: a settable url, recorded navigations, fast waits."""

    def __init__(self, url):
        self.url = url
        self.gotos = []
        self.go_backs = 0
        self.mouse = mock.Mock()
        self.keyboard = mock.Mock()

    def wait_for_timeout(self, ms):
        time.sleep(min(ms / 1000.0, 0.02))

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def screenshot(self, **kwargs):
        return FAKE_JPEG

    def go_back(self, **kwargs):
        self.go_backs += 1

    def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url


class RefusedErrorPageTests(unittest.TestCase):
    """The refused-navigation recovery is one-shot per refusal: a refusal's chrome-error:// page can
    commit late and must put the user back on the last approved page, but a LATER chrome-error:// page
    (e.g. a plain network error on an approved site) must not send the user back again. Regression test
    for the late-commit race - no real Chromium needed."""

    def test_late_error_page_is_put_back_once(self):
        approved_url = 'http://site-a.test/home'
        error_url = 'chrome-error://chromewebdata/'
        page = _FakePage(approved_url)
        session = agent_browser.AgentBrowserSession()
        self.addCleanup(session.close)
        session._page = page
        session._state = lambda approved, images=True: {'snapshot': {'url': page.url}, 'image': None}
        handle = _FakeHandle()

        def script():
            time.sleep(0.15)                                   # the loop settles on the approved page
            session._blocked = ('unapproved', 'site-b.test')   # the route guard refuses the user's click
            time.sleep(0.4)                                    # refusal handled while the old page still shows
            page.url = error_url                               # Chromium commits the error page LATE
            time.sleep(0.5)                                    # the loop navigates back, once
            page.url = error_url                               # a later, unrelated error on the approved site
            time.sleep(0.4)
            handle.done.set()

        thread = threading.Thread(target=script, daemon=True)
        thread.start()
        with mock.patch.object(agent_browser, 'request_allowed',
                               side_effect=lambda url: url.startswith('http://site-a.test/')):
            state = session.takeover(handle, {'site-a.test'}, images=False)
        thread.join(5)

        self.assertEqual(state['status'], 'done')
        self.assertEqual(page.gotos, [approved_url],           # put back for the refusal's error page,
                         'but not again for the unrelated one')
        self.assertEqual(page.url, error_url)                  # the later error page is left alone
        self.assertIn('site-b.test', state.get('blocked_hosts', []))

    def _run(self, page, script, window):
        """Drive session.takeover on ``page`` while ``script(session, handle)`` plays the browser's part,
        with TAKEOVER_REFUSED_WINDOW patched to ``window`` seconds."""
        session = agent_browser.AgentBrowserSession()
        self.addCleanup(session.close)
        session._page = page
        session._state = lambda approved, images=True: {'snapshot': {'url': page.url}, 'image': None}
        handle = _FakeHandle()
        thread = threading.Thread(target=script, args=(session, handle), daemon=True)
        thread.start()
        with mock.patch.object(agent_browser, 'TAKEOVER_REFUSED_WINDOW', window), \
                mock.patch.object(agent_browser, 'request_allowed',
                                  side_effect=lambda url: url.startswith('http://site-a.test/')):
            state = session.takeover(handle, {'site-a.test'}, images=False)
        thread.join(5)
        return state

    def test_refusal_without_error_page_expires(self):
        """A refusal whose error page never commits must not arm the recovery forever: an unrelated
        chrome-error:// page after the window (a network error on the approved site) is left alone."""
        error_url = 'chrome-error://chromewebdata/'
        page = _FakePage('http://site-a.test/home')

        def script(session, handle):
            time.sleep(0.15)                                   # the loop settles on the approved page
            session._blocked = ('unapproved', 'site-b.test')   # refused, but NO error page ever commits
            time.sleep(0.2)
            page.url = 'http://site-a.test/other'              # the user keeps browsing the approved site
            time.sleep(0.6)                                    # ... well past the (patched) 0.3 s window
            page.url = error_url                               # an unrelated network error on site A
            time.sleep(0.4)
            handle.done.set()

        state = self._run(page, script, window=0.3)
        self.assertEqual(state['status'], 'done')
        self.assertEqual(page.gotos, [], 'an error page after the window is not the refusal\'s own')
        self.assertEqual(page.url, error_url)
        self.assertIn('site-b.test', state.get('blocked_hosts', []))

    def test_failed_put_back_is_retried_within_window(self):
        """When putting the user back fails (the page still shows chrome-error://), the loop retries while
        the refusal's window is open, then stops: a later unrelated error page is left alone."""
        approved_url = 'http://site-a.test/home'
        error_url = 'chrome-error://chromewebdata/'

        class _FlakyGotoPage(_FakePage):
            failures = 1

            def goto(self, url, **kwargs):
                self.gotos.append(url)
                if self.failures:
                    self.failures -= 1                          # first attempt fails: still the error page
                    return
                self.url = url

        page = _FlakyGotoPage(approved_url)

        def script(session, handle):
            time.sleep(0.15)
            session._blocked = ('unapproved', 'site-b.test')
            time.sleep(0.2)
            page.url = error_url                               # the refusal's error page commits late
            time.sleep(0.5)                                    # first put-back fails, the retry succeeds
            page.url = error_url                               # a later, unrelated error on site A
            time.sleep(0.4)
            handle.done.set()

        state = self._run(page, script, window=2.0)
        self.assertEqual(state['status'], 'done')
        self.assertEqual(page.gotos, [approved_url, approved_url], 'one failed attempt, one retry, no more')
        self.assertEqual(page.url, error_url)


class _TakeoverHandler(test_agent_browse._Handler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/login':
            body = PAGE_LOGIN.format(port=self.server.port).encode()
        elif parsed.path == '/welcome':
            user = urllib.parse.parse_qs(parsed.query).get('user', [''])[0]
            body = ('<html><head><title>Welcome</title></head><body><h1>Welcome %s</h1></body></html>'
                    % html.escape(user)).encode()
        else:
            return super().do_GET()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@_cap_gate(_LIVE, 'live Chromium test needs playwright + chromium installed')
class LiveTakeoverTests(unittest.TestCase):
    """Real Chromium in the bubblewrap sandbox: the user's clicks and typing reach the page, frames are JPEG,
    and a click toward an unapproved host is refused and reported, not followed."""

    A = test_agent_browse.LiveChromiumTests.A
    B = test_agent_browse.LiveChromiumTests.B

    @classmethod
    def setUpClass(cls):
        cls.server = test_agent_browse._Server(('127.0.0.1', 0), _TakeoverHandler)
        cls.server.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_a = 'http://%s:%d' % (cls.A, cls.server.port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    setUp = test_agent_browse.LiveChromiumTests.setUp

    def drive(self, handle, *events, settle=2.0):
        def run():
            deadline = time.monotonic() + 15
            while handle.get_frame() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            for event in events:
                handle.push(validate_input(event, VIEWPORT))
                time.sleep(0.3)
            time.sleep(settle)
            handle.done.set()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_user_clicks_and_types_into_the_page(self):
        approved = {self.A}
        self.session.open(self.base_a + '/login', approved, False)
        handle = TakeoverBroker().start('alice', 'turn1', 'Log in', VIEWPORT)
        thread = self.drive(handle, {'type': 'click', 'x': 150, 'y': 120}, {'type': 'type', 'text': 'jazz'},
                            {'type': 'key', 'key': 'Enter'})
        state = self.session.takeover(handle, approved, False)
        thread.join(20)
        self.assertEqual(state['status'], 'done')
        self.assertEqual(state['snapshot']['title'], 'Welcome')
        self.assertIn('Welcome jazz', state['snapshot']['text'])
        frame, seq = handle.get_frame()
        self.assertTrue(frame.startswith(b'\xff\xd8'), 'live-view frames are JPEG')
        self.assertGreaterEqual(seq, 1)

    def test_click_to_unapproved_host_is_refused_and_reported(self):
        approved = {self.A}
        self.session.open(self.base_a + '/login', approved, False)
        handle = TakeoverBroker().start('alice', 'turn1', 'Log in', VIEWPORT)
        thread = self.drive(handle, {'type': 'click', 'x': 150, 'y': 320})
        state = self.session.takeover(handle, approved, False)
        thread.join(20)
        self.assertEqual(state['status'], 'done')
        self.assertEqual(state['snapshot']['title'], 'Login')
        self.assertIn(self.B, state.get('blocked_hosts', []))

    def test_stop_and_timeout(self):
        approved = {self.A}
        self.session.open(self.base_a + '/login', approved, False)
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        handle = TakeoverBroker().start('alice', 'turn1', 'Log in', VIEWPORT)
        self.assertEqual(self.session.takeover(handle, approved, False, stop), {'status': 'stopped'})
        with mock.patch.object(config, 'AGENT_BROWSER_TAKEOVER_TIMEOUT', 1):
            handle = TakeoverBroker().start('alice', 'turn1', 'Log in', VIEWPORT)
            state = self.session.takeover(handle, approved, False)
        self.assertEqual(state['status'], 'timeout')
        self.assertEqual(state['snapshot']['title'], 'Login')


if __name__ == '__main__':
    unittest.main()
