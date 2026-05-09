"""Pytest fixtures for tradar smoke tests.

The fixtures stand up a Flask app against a temporary SQLite database so tests
never touch the real `data/app.db`. The app is created exactly once per test
session — that's enough since smoke tests only check routing and template
compilation, not stateful behavior.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make `webapp` importable when pytest runs from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="session")
def app():
    # Force a temp DB so we never write to the real one.
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    os.environ["TRADAR_TEST_DB"] = tmp_db.name

    # Suppress in-process scheduler — tests don't want background tick threads
    # racing the test DB.
    os.environ.pop("WERKZEUG_RUN_MAIN", None)
    os.environ.pop("STOCK_RUN_SCHEDULER", None)

    from webapp import create_app
    app = create_app()
    # Override the SQLite URI to the temp DB. The fixture path is too late to
    # influence the module-level DB_PATH constant, so we patch the live config.
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_db.name}"
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost.test"

    with app.app_context():
        from webapp.models import db
        db.create_all()

    yield app

    # Cleanup
    try:
        os.unlink(tmp_db.name)
    except OSError:
        pass


@pytest.fixture
def client(app):
    return app.test_client()
