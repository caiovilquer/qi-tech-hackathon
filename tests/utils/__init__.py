from os import environ

from tests.utils.db_utils import DbUtils

INTERNAL_TOKEN = environ.get("INTERNAL_TOKEN", "default_token")
