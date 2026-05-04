"""Entry point for the Flask dev server.

Usage:
    python run.py                 # default 127.0.0.1:5050
    HOST=0.0.0.0 PORT=5050 python run.py
"""

from __future__ import annotations

import os

from webapp import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5050"))
    app.run(host=host, port=port, debug=True)
