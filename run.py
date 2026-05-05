"""Entry point for the Flask dev server.

Usage:
    python run.py                 # default 127.0.0.1:7550
    HOST=0.0.0.0 PORT=7550 python run.py

If the requested port is already in use, this script walks forward to find
the first free port (up to 50 attempts) and prints the actual port chosen.
"""

from __future__ import annotations

import os
import socket

from webapp import create_app


def _find_free_port(host: str, start_port: int, max_attempts: int = 50) -> int:
    """Return the first port >= start_port on `host` that we can bind to.
    Raises RuntimeError if nothing in the window is free.
    """
    for offset in range(max_attempts):
        candidate = start_port + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError(
        f"no free port in [{start_port}, {start_port + max_attempts}) on {host}"
    )


app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    requested_port = int(os.environ.get("PORT", "7550"))

    # Flask's debug reloader spawns a child process where it MUST bind to the
    # exact same port the parent picked — otherwise the user's URL changes
    # mid-session. We propagate the resolved port via WERKZEUG_SERVER_FD's
    # sibling env var: the parent finds a free port, exports it, and the child
    # re-reads PORT (now pinned) instead of probing again.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        port = requested_port  # child: trust the port the parent already chose
    else:
        port = _find_free_port(host, requested_port)
        if port != requested_port:
            print(f"[tradar] port {requested_port} busy — using {port} instead", flush=True)
        os.environ["PORT"] = str(port)  # pin for the reloader child

    # Debug mode is OFF by default — when on, Werkzeug exposes an interactive
    # Python REPL via the browser on any error (full RCE). Only enable for
    # local dev via FLASK_DEBUG=1. Docker / production must NEVER set this.
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes", "on")
    app.run(host=host, port=port, debug=debug)
