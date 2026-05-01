"""Android entry point. Started by MainActivity via Chaquopy.

Spawns the Flask app on 127.0.0.1:5000 in a background thread (using
werkzeug.serving.make_server, which works fine off the main thread —
unlike app.run() which tries to install signal handlers), then blocks
until the socket is accepting so the Java side knows when to load
the WebView.
"""

import socket
import threading
import time


def start():
    from werkzeug.serving import make_server
    from web_app import app

    server = make_server('127.0.0.1', 5000, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True, name='flask').start()

    # Wait up to 15s for the socket to accept.
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', 5000), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False
