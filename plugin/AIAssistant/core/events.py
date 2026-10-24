"""Thread-safe event bus (Python -> SPA) and a small in-memory blob store.

Events carry an increasing id so a reconnecting client (SSE Last-Event-ID) can
replay what it missed from the ring buffer.
"""
import collections
import threading
import time
import uuid


class EventBus(object):
    def __init__(self, history=1000):
        self._cond = threading.Condition(threading.Lock())
        self._events = collections.deque(maxlen=history)
        self._next_id = 1
        self._listeners = []

    def publish(self, event_type, data=None):
        with self._cond:
            event = {"id": self._next_id, "type": event_type, "data": data, "ts": time.time()}
            self._next_id += 1
            self._events.append(event)
            self._cond.notify_all()
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                pass
        return event

    def addListener(self, callback):
        with self._cond:
            self._listeners.append(callback)

    def lastId(self):
        with self._cond:
            return self._next_id - 1

    def _since(self, last_id):
        return [e for e in self._events if e["id"] > last_id]

    def wait(self, last_id, timeout):
        """Return events newer than last_id, waiting up to timeout seconds for one."""
        with self._cond:
            events = self._since(last_id)
            if events:
                return events
            self._cond.wait(timeout)
            return self._since(last_id)


class BlobStore(object):
    """Keeps binary payloads (snapshots, thumbnails) out of the event stream."""

    def __init__(self, capacity=60):
        self._lock = threading.Lock()
        self._items = collections.OrderedDict()
        self._capacity = capacity

    def put(self, data, mime):
        blob_id = uuid.uuid4().hex[:16]
        with self._lock:
            self._items[blob_id] = (mime, data)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
        return blob_id

    def get(self, blob_id):
        with self._lock:
            return self._items.get(blob_id)
