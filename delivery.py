"""Durable outbox. Telegram has no idempotency key: ambiguous sends may repeat.

Only public catalogue event data is queued; rendered private fit context is never
persisted. GitHub Contents CAS and a bounded lease serialize disposable runners.
"""
import base64
import copy
import fcntl
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
import price_history


class JournalError(RuntimeError):
    pass


class Deferred(RuntimeError):
    """Request was not attempted (or an explicit response deferred it)."""


class Rejected(Deferred):
    """Telegram explicitly rejected this send; safe to retry later."""


class RateLimited(Deferred):
    def __init__(self, seconds):
        self.seconds = max(1, int(seconds))
        super().__init__('Telegram rate limited; delivery deferred')


class LocalBackend:
    def __init__(self, path):
        self.path = path
        self.lock = open(path + '.lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise JournalError('Another tracker holds the delivery journal') from None

    def read(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {'version': 1, 'events': {}}

    def write(self, data):
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        directory = os.open(os.path.dirname(self.path) or '.', os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def close(self):
        self.lock.close()


class GitHubBackend:
    def __init__(self, repository, branch, token, source_sha):
        self.root = 'https://api.github.com/repos/' + repository
        self.branch, self.token, self.sha = branch, token, None
        # Create only the dedicated journal branch; never overwrite an existing ref.
        try:
            self.request('/git/refs/heads/' + branch)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise JournalError('Cannot read delivery journal branch') from None
            try:
                self.request('/git/refs', 'POST', {'ref': 'refs/heads/' + branch, 'sha': source_sha})
            except urllib.error.HTTPError as e:
                if e.code != 422:  # a concurrent initializer may have won
                    raise JournalError('Cannot initialize delivery journal branch') from None
                self.request('/git/refs/heads/' + branch)

    def request(self, path, method='GET', data=None):
        req = urllib.request.Request(self.root + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': 'Bearer ' + self.token,
                     'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'User-Agent': 'shopify-tracker'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)

    def read(self):
        try:
            response = self.request('/contents/delivery.json?ref=' + self.branch)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise JournalError('Cannot read delivery journal') from None
            self.sha = None
            return {'version': 1, 'events': {}}
        self.sha = response['sha']
        content = response.get('content')
        if not content:
            content = self.request('/git/blobs/' + self.sha)['content']
        return json.loads(base64.b64decode(content))

    def write(self, data):
        body = {'message': 'Checkpoint delivery journal', 'branch': self.branch,
                'content': base64.b64encode(json.dumps(data).encode()).decode()}
        if self.sha:
            body['sha'] = self.sha
        try:
            result = self.request('/contents/delivery.json', 'PUT', body)
            self.sha = result['content']['sha']
        except Exception:
            # CAS conflict or uncertain write: stop; do not send from stale state.
            raise JournalError('Delivery journal checkpoint failed; stopping sends') from None

    def close(self):
        pass


class Outbox:
    def __init__(self, backend, seconds=240):
        self.backend = backend
        self.started_at = time.monotonic()
        self.deadline = self.started_at + seconds
        self.owner = uuid.uuid4().hex
        self.data = backend.read()
        if self.data.get('version') != 1 or not isinstance(self.data.get('events'), dict):
            raise JournalError('Invalid delivery journal; refusing to reset it')
        if self.data.get('lease', {}).get('until', 0) > time.time():
            backend.close()
            raise JournalError('Another tracker holds the delivery journal lease')
        self.data['lease'] = {'owner': self.owner, 'until': time.time() + 900}
        self.checkpoint()
        self.sent = 0

    def checkpoint(self):
        if getattr(self, 'failed', False):
            raise JournalError('Delivery journal is unavailable; stopping writes')
        try:
            self.backend.write(self.data)
        except Exception:
            self.failed = True
            raise

    def close(self):
        try:
            if not getattr(self, 'failed', False):
                self.data.pop('lease', None)
                self.checkpoint()
        finally:
            self.backend.close()

    def queue(self, event_id, payload):
        self.queue_many([(event_id, payload)])

    def queue_many(self, events, domain=None, observations=None):
        changed = False
        for event_id, payload in events:
            if event_id not in self.data['events']:
                self.data['events'][event_id] = {'status': 'pending', 'payload': copy.deepcopy(payload)}
                changed = True
        if observations:
            saved = self.data.setdefault('observations', {}).setdefault(domain, {})
            for pid, observation in observations.items():
                observation = copy.deepcopy(observation)
                old_cursor = saved.get(pid, {})
                if ('_revision' in old_cursor and
                        observation.get('_revision', -1) <= old_cursor['_revision']):
                    continue
                old_prices = saved.get(pid, {}).get('prices', {})
                new_prices = observation.get('prices', {})
                if (price_history.META in old_prices and
                        old_prices[price_history.META]['revision'] >
                        new_prices.get(price_history.META, {}).get('revision', -1)):
                    # A stale migration/observation must not undo a newer send
                    # checkpoint. Keep the authoritative durable price cursor.
                    continue
                if saved.get(pid) != observation:
                    saved[pid] = copy.deepcopy(observation)
                    changed = True
        if changed:
            # The event, next sequence and stock/price observation commit together.
            self.checkpoint()  # whole store is durable before advancing observations

    def deliver(self, event_id, send, confirmed=None):
        event = self.data['events'][event_id]
        now = time.time()
        if event['status'] == 'sent' or event.get('suppressed'):
            return False
        if time.monotonic() + 25 >= self.deadline:
            raise Deferred('Delivery budget exhausted')
        if max(self.data.get('retry_at', 0), event.get('retry_at', 0)) > now:
            raise Deferred('Delivery deferred until retry time')
        if event['status'] == 'sending':
            print('Delivery recovery: retrying an ambiguous event; a duplicate is possible')
        event['status'] = 'sending'
        event['retry_at'] = now + 900
        self.checkpoint()  # cancellation after this is an ambiguous send, never a confirmed success
        try:
            result = send()
        except RateLimited as e:
            event['status'] = 'pending'
            event['retry_at'] = time.time() + e.seconds
            self.data['retry_at'] = time.time() + e.seconds
            self.checkpoint()
            raise
        except Rejected:
            event['status'] = 'pending'
            event['retry_at'] = time.time() + 60
            self.checkpoint()
            raise
        except JournalError:
            raise
        except Deferred:
            # The caller's budget/cooldown preflight made no HTTP request.
            event['status'] = 'pending'
            event.pop('retry_at', None)
            self.checkpoint()
            raise
        except Exception:
            # Includes transport timeout after Telegram may have accepted it.
            # Retain payload and retry after 15 minutes; do not log URLs/tokens.
            event['status'] = 'sending'
            self.checkpoint()
            raise Deferred('Telegram outcome uncertain; retry deferred for 15 minutes') from None
        if confirmed:
            confirmed()
        event.clear()
        event.update(status='sent', message_id=result, sent_at=int(time.time()))
        self.checkpoint()  # every confirmed send survives failure of the main history commit
        self.sent += 1
        print('Delivery confirmed: ' + event_id)
        return True


def event_key(domain, pid, sequence):
    return hashlib.sha256(json.dumps([domain, str(pid), sequence]).encode()).hexdigest()[:32]


def open_outbox(base):
    branch = os.environ.get('TRACKER_JOURNAL_BRANCH')
    if os.environ.get('GITHUB_ACTIONS') == 'true' and not branch:
        raise JournalError('Actions delivery requires a durable remote journal')
    if branch:
        backend = GitHubBackend(os.environ['GITHUB_REPOSITORY'], branch,
                                os.environ['GITHUB_TOKEN'], os.environ['GITHUB_SHA'])
    else:
        backend = LocalBackend(os.path.join(base, 'delivery.json'))
    return Outbox(backend)
