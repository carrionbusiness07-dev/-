"""Запуск кабинета: `python -m minsk_realty.web` → http://localhost:8080."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "minsk_realty.web.app:app",
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEB_PORT", "8080")),
    )
