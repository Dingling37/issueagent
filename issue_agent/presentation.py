"""Best-effort application boundary for optional, read-only presentation."""
import json
from .security import redact


class Presentation:
    def __init__(self, factory, tracer):
        self.observer = None
        self.unsubscribe = None
        self.failed = False
        self.closed = False
        if factory is None:
            return
        try:
            self.observer = factory()
            if self.observer is not None:
                self.observer.start()
                self.unsubscribe = tracer.subscribe(self.on_event)
        except Exception:
            self.failed = True
            self.close()

    def on_event(self, event):
        if self.failed:
            return
        try:
            self.observer.on_event(event)
        except Exception:
            self.failed = True
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.unsubscribe is not None:
            self.unsubscribe()
            self.unsubscribe = None
        if self.observer is not None:
            try:
                self.observer.close()
            except Exception:
                self.failed = True

    def finish(self, result):
        self.close()
        if self.observer is None or self.failed:
            return False
        try:
            # Never give presentation code the Agent result or state by reference.
            snapshot = json.loads(redact(json.dumps(result, ensure_ascii=False)))
            return self.observer.finish(snapshot) is True
        except Exception:
            self.failed = True
            return False
