"""Entry point for the Flask dev server.

Usage:
    python run.py                 # default 127.0.0.1:7551
    HOST=0.0.0.0 PORT=7551 python run.py
"""

from __future__ import annotations

import logging
import os

# Configure logging BEFORE app creation so every module's getLogger() inherits
# the timestamp format. Previously log.info/warning calls landed in run.log
# without dates, making it impossible to grep by time when debugging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,  # override any prior config from imports
)

from webapp import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "7551"))
    app.run(host=host, port=port, debug=True)
