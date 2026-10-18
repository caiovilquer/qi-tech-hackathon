import argparse
import os

from sqlalchemy import create_engine

from migrations.runner import upgrade


def main():
    parser = argparse.ArgumentParser(description="Apply pending PostgreSQL migrations")
    parser.add_argument("--adopt-base", action="store_true",
                        help="Validate and adopt an existing unversioned Base API schema")
    args = parser.parse_args()
    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        applied = upgrade(engine, adopt_base=args.adopt_base)
        print("Applied: " + (", ".join(applied) if applied else "already current"))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
