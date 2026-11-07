"""Browser take-over: hand the agent's live browser to the user and wait for "Done".

The agent calls ``request_takeover`` (agenticbot/tools/browse.py) when a page needs a person:
a captcha, a login, a payment or a consent screen. The turn pauses the same way an
approval does. While it waits, the agent's browser worker thread pumps a loop
(``AgentBrowserSession.takeover``) that publishes JPEG frames of the page into the
``Takeover`` object and replays the user's clicks, keys and scrolls from its input queue.
The web UI shows the frames as a live view and posts the user's input back.

Safety shape:

* Owner-scoped like approvals: every lookup checks the session id, so one browser session
  cannot watch or drive another owner's take-over.
* Nothing the user does reaches the model. Input events and frames are never emitted as
  turn events, persisted or logged; after "Done" the agent only gets a fresh snapshot of the
  page, the same as browse_see. Typed text (a password) stays between the UI and Chromium.
* The network rules do not change: the vetting proxy and route guard still refuse hosts the
  user has not approved, so a take-over cannot browse to an unapproved site either.
* Bounded: input is validated (event types, coordinates inside the viewport, text length,
  an allowlist of keys) and queued with a cap, and every take-over ends on Done, Stop, the
  turn ending, or ``AGENTIC_AGENT_BROWSER_TAKEOVER_TIMEOUT`` seconds.
"""
import collections
import secrets
import threading
import time

MAX_TEXT = 500                    # characters per 'type' event
MAX_QUEUE = 200                   # pending input events per take-over
MAX_SCROLL = 3000                 # pixels per 'scroll' event
KEYS = ('Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
        'Home', 'End', 'PageUp', 'PageDown', 'Space')


class Takeover:
    """One pending take-over: the user's input queue one way, the latest frame the other."""

    def __init__(self, session_id, turn_id, reason, viewport):
        self.id = secrets.token_hex(8)
        self.session_id = session_id
        self.turn_id = turn_id
        self.reason = reason
        self.viewport = dict(viewport)
        self.created = time.time()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.inputs = collections.deque()
        self.frame = None                # (jpeg bytes, sequence number)
        self.seq = 0

    def push(self, event):
        with self.lock:
            if len(self.inputs) >= MAX_QUEUE:
                return False
            self.inputs.append(event)
            return True

    def drain(self):
        with self.lock:
            items = list(self.inputs)
            self.inputs.clear()
            return items

    def set_frame(self, jpeg):
        with self.lock:
            self.seq += 1
            self.frame = (jpeg, self.seq)

    def get_frame(self):
        with self.lock:
            return self.frame

    def public(self):
        return {'id': self.id, 'reason': self.reason, 'turn_id': self.turn_id, 'viewport': dict(self.viewport)}


def validate_input(event, viewport):
    """A clean input event, or ValueError. Only what the live view needs: click, type, key, scroll."""
    if not isinstance(event, dict):
        raise ValueError('Send a JSON object.')
    kind = event.get('type')
    if kind == 'click':
        x, y = event.get('x'), event.get('y')
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y)):
            raise ValueError('click needs numeric x and y.')
        if not (0 <= x <= viewport['width'] and 0 <= y <= viewport['height']):
            raise ValueError('click is outside the page.')
        return {'type': 'click', 'x': float(x), 'y': float(y)}
    if kind == 'type':
        text = event.get('text')
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            raise ValueError('type needs 1-%d characters of text.' % MAX_TEXT)
        return {'type': 'type', 'text': text}
    if kind == 'key':
        key = event.get('key')
        if key not in KEYS:
            raise ValueError('Unsupported key.')
        return {'type': 'key', 'key': key}
    if kind == 'scroll':
        dy = event.get('dy')
        if not isinstance(dy, (int, float)) or isinstance(dy, bool) or abs(dy) > MAX_SCROLL:
            raise ValueError('scroll needs dy between -%d and %d.' % (MAX_SCROLL, MAX_SCROLL))
        return {'type': 'scroll', 'dy': float(dy)}
    raise ValueError('Unknown input type.')


class TakeoverBroker:
    """Pending take-overs by id, scoped to the owner's session id (like ApprovalBroker)."""

    def __init__(self):
        self.active = {}
        self.lock = threading.Lock()

    def start(self, session_id, turn_id, reason, viewport):
        takeover = Takeover(session_id, turn_id, reason, viewport)
        with self.lock:
            self.active[takeover.id] = takeover
        return takeover

    def end(self, takeover_id):
        with self.lock:
            takeover = self.active.pop(takeover_id, None)
        if takeover is not None:
            takeover.done.set()

    def get(self, takeover_id, session_id):
        with self.lock:
            takeover = self.active.get(takeover_id)
        if takeover is None or takeover.session_id != session_id:
            return None
        return takeover

    def finish(self, takeover_id, session_id):
        """The user tapped Done. False when there is no such take-over for this owner."""
        takeover = self.get(takeover_id, session_id)
        if takeover is None:
            return False
        takeover.done.set()
        return True

    def send_input(self, takeover_id, session_id, event):
        """Queue one validated input event. None: no such take-over; False: queue full."""
        takeover = self.get(takeover_id, session_id)
        if takeover is None:
            return None
        return takeover.push(validate_input(event, takeover.viewport))

    def list_active(self, session_id):
        with self.lock:
            return [t.public() for t in self.active.values() if t.session_id == session_id]
