from pathlib import Path
from os import path, environ

import pytest
from sqlalchemy.engine import make_url

root = Path(__file__).resolve().parents[1]

if not environ.get("APP_ENV") or environ.get("APP_ENV") == "local":
    from dotenv import load_dotenv

    load_dotenv(path.join(str(root), ".env"))

    if environ.get("SERVER_LOCALHOST") is None:
        environ["SERVER_LOCALHOST"] = "0.0.0.0"


def pytest_sessionstart(session):
    database_url = environ.get("DATABASE_URL")
    if (environ.get("APP_ENV") != "testing" or not database_url
            or not (make_url(database_url).database or "").startswith("pix_test_")):
        raise pytest.UsageError(
            "Tests require APP_ENV=testing and a dedicated DATABASE_URL whose "
            "database name starts with pix_test_. Use compose.test.yml."
        )
