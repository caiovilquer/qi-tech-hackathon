from pathlib import Path
from os import path, environ

import pytest

root = Path(__file__).resolve().parents[1]

if not environ.get("APP_ENV") or environ.get("APP_ENV") == "local":
    from dotenv import load_dotenv

    load_dotenv(path.join(str(root), ".env"))


def pytest_sessionstart(session):
    from tests.utils.environment import verify_api_database

    try:
        verify_api_database()
    except Exception as error:
        raise pytest.UsageError(
            "Test environment verification failed; no tests were run. "
            "Use compose.test.yml. " + str(error)
        ) from error
