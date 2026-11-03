"""Создать базу аренды (RENT_DATABASE_URL) рядом с основной, если её ещё нет.

Запускает сервис migrate перед `alembic upgrade head` для аренды. PostgreSQL создаёт
только базу из POSTGRES_DB при первом запуске тома, вторую нужно создать самим.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main() -> int:
    rent_url = os.environ.get("RENT_DATABASE_URL")
    if not rent_url:
        print("RENT_DATABASE_URL не задан — база аренды не нужна")
        return 0
    rent = make_url(rent_url)
    if rent.get_backend_name() != "postgresql":
        return 0  # SQLite создаёт файл сам
    engine = create_engine(os.environ["DATABASE_URL"], isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": rent.database}
        )
        if not exists:
            # Имя базы — из нашего же compose-файла, не из ввода пользователя.
            conn.execute(text(f'CREATE DATABASE "{rent.database}"'))
            print(f"создана база {rent.database}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
