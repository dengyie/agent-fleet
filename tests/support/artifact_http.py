"""Disposable loopback HTTP server for the Node artifact integration tests."""
from contextlib import contextmanager
import threading
from typing import Any, Iterator

from werkzeug.serving import make_server


@contextmanager
def server(app: Any) -> Iterator[str]:
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
        assert not thread.is_alive()
