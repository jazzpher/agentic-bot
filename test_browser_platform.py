"""Offline tests for the agent browser's platform handling: which isolation mode each OS gets,
the proxy-mode argument guard, the startup proxy self-test and cross-platform process cleanup.
No Chromium needed; runs on Linux, Windows and macOS."""
import os
import subprocess
import unittest
from unittest import mock

from agenticbot import agent_browser, config
from agenticbot.agent_browser import BrowserError


class IsolationModeTests(unittest.TestCase):
    def mode(self, platform, setting, override=None):
        with mock.patch.object(agent_browser.sys, 'platform', platform), \
                mock.patch.object(config, 'AGENT_BROWSER_ISOLATION', setting):
            return agent_browser._isolation_mode(override)

    def test_linux_always_uses_the_namespace(self):
        self.assertEqual(self.mode('linux', 'namespace'), 'namespace')
        self.assertEqual(self.mode('linux', 'proxy'), 'namespace', 'Linux must never downgrade to proxy')

    def test_windows_and_mac_need_an_explicit_opt_in(self):
        for platform in ('win32', 'darwin'):
            with self.assertRaises(BrowserError) as ctx:
                self.mode(platform, 'namespace')
            self.assertIn('bubblewrap', str(ctx.exception))
            self.assertIn('AGENTIC_AGENT_BROWSER_ISOLATION=proxy', str(ctx.exception))
            self.assertEqual(self.mode(platform, 'proxy'), 'proxy')

    def test_other_platforms_refuse_even_with_proxy(self):
        with self.assertRaises(BrowserError):
            self.mode('freebsd13', 'proxy')

    def test_constructor_override(self):
        self.assertEqual(self.mode('linux', 'namespace', 'proxy'), 'proxy')
        with self.assertRaises(BrowserError):
            self.mode('linux', 'namespace', 'off')

    def test_config_parses_the_setting(self):
        name = 'AGENTIC_AGENT_BROWSER_ISOLATION'
        for raw, want in (('', 'namespace'), ('proxy', 'proxy'), (' PROXY ', 'proxy'),
                          ('namespace', 'namespace'), ('none', 'namespace')):
            with mock.patch.dict(os.environ, {name: raw}):
                self.assertEqual(config._choice(name, 'namespace', ('namespace', 'proxy')), want, raw)
        self.assertIn(config.AGENT_BROWSER_ISOLATION, ('namespace', 'proxy'))


class ProxyArgsTests(unittest.TestCase):
    def test_rerouting_switches_are_refused(self):
        for switch in agent_browser._PROXY_REROUTING_SWITCHES:
            for arg in (switch, switch + '=http://evil:1', switch.upper() + '=x'):
                with self.assertRaises(BrowserError, msg=arg):
                    agent_browser._refuse_rerouting_args(['--headless=new', arg])

    def test_ordinary_args_pass(self):
        agent_browser._refuse_rerouting_args(['--ignore-certificate-errors', '--lang=en-US',
                                              '--window-size=1280,800'])

    def test_hardened_args(self):
        args = agent_browser._PROXY_ARGS
        self.assertIn('--disable-quic', args)
        self.assertIn('--proxy-bypass-list=<-loopback>', args)
        self.assertTrue(any(a.startswith('--host-resolver-rules=MAP * ~NOTFOUND') for a in args))
        self.assertIn('--force-webrtc-ip-handling-policy=disable_non_proxied_udp', args)


class _FakeContext:
    def __init__(self, on_goto):
        self.on_goto = on_goto
        self.closed = False
        self.urls = []

    def new_page(self):
        ctx = self

        class Page:
            def goto(self, url, timeout=None):
                ctx.urls.append(url)
                ctx.on_goto(url)
                raise RuntimeError('net::ERR_TUNNEL_CONNECTION_FAILED')
        return Page()

    def close(self):
        self.closed = True


class _FakeProxy:
    def __init__(self):
        self.denials = []

    def drain_denials(self):
        out, self.denials = self.denials, []
        return out


class VerifyProxyTransportTests(unittest.TestCase):
    def session(self, proxy_used):
        session = agent_browser.AgentBrowserSession(isolation='proxy')
        session._proxy = _FakeProxy()

        def on_goto(url):
            if proxy_used:
                host = url.split('//', 1)[1].split('/', 1)[0]
                session._proxy.denials.append((host, 80, 'unapproved host'))
        self.ctx = _FakeContext(on_goto)
        session._browser = mock.Mock(new_context=mock.Mock(return_value=self.ctx))
        return session

    def test_passes_when_the_proxy_refused_the_probe(self):
        session = self.session(proxy_used=True)
        session._verify_proxy_transport()
        self.assertTrue(self.ctx.closed)
        self.assertEqual(len(self.ctx.urls), 1)
        self.assertRegex(self.ctx.urls[0], r'^http://grok-proxy-probe-[0-9a-f]{32}\.invalid/$')

    def test_refuses_when_chromium_bypassed_the_proxy(self):
        session = self.session(proxy_used=False)
        with self.assertRaises(BrowserError) as ctx:
            session._verify_proxy_transport()
        self.assertIn('vetting proxy', str(ctx.exception))
        self.assertTrue(self.ctx.closed)

    def test_a_different_denial_does_not_count(self):
        session = self.session(proxy_used=False)
        session._proxy.denials.append(('other.invalid', 80, 'unapproved host'))
        with self.assertRaises(BrowserError):
            session._verify_proxy_transport()


class ProcessTreeTests(unittest.TestCase):
    def test_mac_descendants_from_ps(self):
        ps = mock.Mock(stdout=' 10 1\n 11 10\n 12 11\n 13 10\n 99 5\n 10 12\n')   # includes a cycle
        with mock.patch.object(agent_browser.sys, 'platform', 'darwin'), \
                mock.patch.object(agent_browser.subprocess, 'run', return_value=ps) as run:
            got = agent_browser._descendants(10)
        self.assertEqual(run.call_args[0][0][0], 'ps')
        self.assertEqual(sorted(got), [11, 12, 13])
        self.assertLess(got.index(11), got.index(12), 'parents before children')

    def test_mac_ps_failure_is_empty(self):
        with mock.patch.object(agent_browser.sys, 'platform', 'darwin'), \
                mock.patch.object(agent_browser.subprocess, 'run',
                                  side_effect=subprocess.TimeoutExpired('ps', 5)):
            self.assertEqual(agent_browser._descendants(10), [])

    def test_windows_has_no_descendant_walk(self):
        with mock.patch.object(agent_browser.sys, 'platform', 'win32'):
            self.assertEqual(agent_browser._descendants(10), [])

    def test_windows_kill_uses_taskkill_tree(self):
        with mock.patch.object(agent_browser.sys, 'platform', 'win32'), \
                mock.patch.object(agent_browser.subprocess, 'run') as run, \
                mock.patch.object(agent_browser.os, 'kill') as kill:
            agent_browser._kill_process_tree(4321)
        self.assertEqual(run.call_args[0][0], ['taskkill', '/PID', '4321', '/T', '/F'])
        kill.assert_not_called()

    def test_windows_kill_tolerates_a_gone_process(self):
        with mock.patch.object(agent_browser.sys, 'platform', 'win32'), \
                mock.patch.object(agent_browser.subprocess, 'run',
                                  side_effect=subprocess.CalledProcessError(128, 'taskkill')):
            agent_browser._kill_process_tree(4321)

    @unittest.skipIf(os.name == 'nt', 'POSIX signals')
    def test_posix_kill_children_first_then_reap(self):
        killed = []
        with mock.patch.object(agent_browser.sys, 'platform', 'darwin'), \
                mock.patch.object(agent_browser, '_descendants', return_value=[11, 12]), \
                mock.patch.object(agent_browser.os, 'kill', side_effect=lambda p, s: killed.append(p)), \
                mock.patch.object(agent_browser.os, 'waitpid', return_value=(10, 0)) as waitpid:
            agent_browser._kill_process_tree(10)
        self.assertEqual(killed, [12, 11, 10])
        waitpid.assert_called()


if __name__ == '__main__':
    unittest.main()
