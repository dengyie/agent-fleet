"""Bounded structured failures: identities and stack locations, never raw exception text."""
import json
from pathlib import Path
import traceback


def log_failure(logger, code, exc=None, **context):
    record = {'event': code, **context}
    if exc is not None:
        record['exception_type'] = type(exc).__name__
        frames = []
        seen = set()
        record['exception_chain'] = []
        while exc is not None and id(exc) not in seen and len(seen) < 4:
            seen.add(id(exc))
            record['exception_chain'].append(type(exc).__name__)
            frames.extend({'file': Path(frame.filename).name, 'line': frame.lineno,
                           'function': frame.name}
                          for frame in traceback.extract_tb(exc.__traceback__)[-6:])
            exc = exc.__cause__
        record['frames'] = frames[-12:]
    logger.warning('%s', json.dumps(record, ensure_ascii=True, separators=(',', ':')))
