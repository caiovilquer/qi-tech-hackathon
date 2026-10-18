import math
import os


SERVICE_ROOT = os.path.abspath(os.path.dirname(__file__))
SCHEMA_PATH = os.path.join(SERVICE_ROOT, "schemas")

APP_ENV = os.environ.get("APP_ENV", "local")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "bootcamp-api")

DATABASE_URL = os.environ.get("DATABASE_URL")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN")

BANKSLIP_API_URL = os.environ.get("BANKSLIP_API_URL", "http://localhost:8080")
BANKSLIP_API_INTERNAL_TOKEN = os.environ.get("BANKSLIP_API_INTERNAL_TOKEN", "default_token")
BANKSLIP_API_TIMEOUT = float(os.environ.get("BANKSLIP_API_TIMEOUT", "5"))
if not math.isfinite(BANKSLIP_API_TIMEOUT) or BANKSLIP_API_TIMEOUT <= 0:
    raise ValueError("BANKSLIP_API_TIMEOUT must be positive finite seconds")

# Public routes: no INTERNAL-TOKEN and no database session.
BYPASS_ENDPOINTS = ["/", "/health_check"]

REQUIRED_VARIABLES = ["DATABASE_URL", "INTERNAL_TOKEN"]


def check_variables():
    missing = [name for name in REQUIRED_VARIABLES if not globals().get(name)]
    if missing:
        raise EnvironmentError(
            f"Faltam variáveis de ambiente: {', '.join(missing)}. "
            "Rodando com 'docker compose up' elas já vêm preenchidas. "
            "Fora do Docker, copie o .env.example para .env."
        )
