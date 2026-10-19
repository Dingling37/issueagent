"""Process-local secret redaction at logging and artifact output boundaries."""
import json
import logging
import traceback

_secrets = set()
_installed = False


def redact(text):
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
        text = text.replace(json.dumps(secret)[1:-1], "[REDACTED]")
    return text


def protect_secret(secret):
    global _installed
    if not secret:
        return
    _secrets.add(secret)
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.msg, record.args = redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
