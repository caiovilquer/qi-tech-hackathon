from os import environ

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import make_url

from migrations.runner import upgrade


RESET_QUERIES = [
    "DROP SCHEMA public CASCADE;",
    "CREATE SCHEMA public;",
]

DATABASE_OFFLINE = (
    "Não consegui falar com o banco em {host}:{port}.\n"
    "Suba o ambiente isolado de compose.test.yml.\n"
    "Se ele já está de pé, confira a porta na DATABASE_URL do seu .env."
)


class DbUtils:
    """Reset explícito e destrutivo, permitido somente no banco de testes.

    O nome legado rollback() é mantido para a suíte fornecida. Ele derruba
    public e reaplica as migrations, não desfaz uma transação de aplicação.
    """

    @staticmethod
    def database_url() -> str:
        value = environ.get("DATABASE_URL")
        if (environ.get("APP_ENV") != "testing" or not value
                or not (make_url(value).database or "").startswith("pix_test_")):
            raise RuntimeError("Refusing reset outside a dedicated pix_test_ database")
        return value

    @staticmethod
    def rollback() -> None:
        engine = create_engine(DbUtils.database_url())

        try:
            with engine.begin() as connection:
                for query in RESET_QUERIES:
                    connection.exec_driver_sql(query)
            upgrade(engine)
        except OperationalError:
            message = DATABASE_OFFLINE.format(host=engine.url.host, port=engine.url.port)
            raise RuntimeError(message) from None
        finally:
            engine.dispose()
