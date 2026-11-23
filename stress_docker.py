#!/usr/bin/env python3
"""Docker sandbox stress: the backend the docs recommend, executed for real.

`AGENTIC_SANDBOX_BACKEND=docker` is the only boundary the project has for running
commands from an untrusted repository, and no CI job has ever started a daemon for it.
This checks the four properties the documentation claims, against a real container:

    python stress_docker.py

Network off means off, the root filesystem is read-only, the workspace mount is
writable, and Stop kills the container. It exits 2 (not a failure) when there is no
usable Docker, so the same command is safe on a machine without it.
"""
import argparse
import pathlib
import subprocess
import tempfile
import threading
import time

from agenticbot import sandbox

RESULTS = []


def record(name, ok, detail, seconds=0.0):
    RESULTS.append((name, None if ok is None else bool(ok), detail, seconds))
    print('%-40s %-4s %7.2fs  %s' % (name, 'PASS' if ok else ('n/a' if ok is None else 'FAIL'),
                                     seconds, detail))


def run(backend, workspace, name, command, timeout=60, network=False, stop=None):
    started = time.monotonic()
    try:
        result = backend.run(command, workspace, timeout, network=network, stop=stop)
        return result, time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 - the stress reports, it never crashes
        record(name, False, type(exc).__name__ + ': ' + str(exc)[:160], time.monotonic() - started)
        return None, time.monotonic() - started


def check_echo(backend, workspace):
    result, seconds = run(backend, workspace, 'echo runs in the container', 'echo container-hi', timeout=60)
    if result is None:
        return
    record('echo runs in the container', result.exit_code == 0 and 'container-hi' in result.stdout,
           'exit %s, isolated=%s' % (result.exit_code, result.isolated), seconds)


def check_read_only_root(backend, workspace):
    result, seconds = run(backend, workspace, 'root filesystem is read-only', 'touch /probe-should-fail')
    if result is None:
        return
    record('root filesystem is read-only', result.exit_code not in (0, None),
           'exit %s (%s)' % (result.exit_code, (result.stderr or '').strip().splitlines()[-1:] or 'no stderr'),
           seconds)


def check_scratch(backend, workspace):
    result, seconds = run(backend, workspace, '/tmp is writable', 'touch /tmp/probe && echo scratch-ok')
    if result is None:
        return
    record('/tmp is writable', result.exit_code == 0 and 'scratch-ok' in result.stdout,
           'exit %s' % result.exit_code, seconds)


def check_workspace_mount(backend, workspace):
    target = pathlib.Path(workspace) / 'from-container.txt'
    result, seconds = run(backend, workspace, 'workspace mount is writable', 'echo mounted > from-container.txt')
    if result is None:
        return
    written = target.is_file() and target.read_text(encoding='utf-8').strip() == 'mounted'
    record('workspace mount is writable', result.exit_code == 0 and written,
           'exit %s, file on host=%s' % (result.exit_code, written), seconds)


def check_network_off(backend, workspace):
    probe = ("python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 3); print('REACHED')\"")
    result, seconds = run(backend, workspace, 'network is off by default', probe, timeout=45)
    if result is None:
        return
    record('network is off by default', result.exit_code not in (0, None) and 'REACHED' not in result.stdout,
           'exit %s, stdout=%r' % (result.exit_code, (result.stdout or '').strip()[:60]), seconds)


def check_stop_kills(backend, workspace):
    stop = threading.Event()
    timer = threading.Timer(1.5, stop.set)
    timer.start()
    try:
        result, seconds = run(backend, workspace, 'Stop kills the container', 'sleep 60', timeout=60, stop=stop)
    finally:
        timer.cancel()
    if result is None:
        return
    record('Stop kills the container', result.stopped and seconds < 30,
           'stopped=%s after %.1fs' % (result.stopped, seconds), seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='', help='sandbox image (default: AGENTIC_SANDBOX_IMAGE)')
    args = parser.parse_args()

    backend = sandbox.DockerBackend(args.image or None)
    print('agenticbot docker sandbox stress (%s)\nimage: %s\n'
          % (time.strftime('%Y-%m-%d %H:%M:%S'), backend.image))
    if not backend.available():
        print('SKIP: no docker CLI on PATH.')
        return 2
    try:
        info = subprocess.run(['docker', 'info'], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print('SKIP: docker is not usable here (%s).' % exc)
        return 2
    if info.returncode != 0:
        print('SKIP: no reachable docker daemon.')
        print((info.stderr or '').strip()[:400])
        return 2

    pull = subprocess.run(['docker', 'pull', backend.image], capture_output=True, text=True, timeout=600)
    print('image ready: %s (%s)\n' % (backend.image, 'pulled' if pull.returncode == 0 else 'pull failed, cached copy'))

    with tempfile.TemporaryDirectory() as tmp:
        workspace = pathlib.Path(tmp)
        check_echo(backend, workspace)
        check_read_only_root(backend, workspace)
        check_scratch(backend, workspace)
        check_workspace_mount(backend, workspace)
        check_network_off(backend, workspace)
        check_stop_kills(backend, workspace)

    failed = sum(1 for row in RESULTS if row[1] is False)
    print('\n%d passed, %d failed' % (len(RESULTS) - failed, failed))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
