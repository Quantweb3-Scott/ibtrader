import logging
import os

import uvicorn

from .config import Settings
from .web import create_app


def run() -> None:
    settings = Settings.load(os.getenv("IBTRADER_CONFIG", "config.yaml"))
    logging.basicConfig(
        level=settings.app.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(create_app(settings), host=settings.app.host, port=settings.app.port)


if __name__ == "__main__":
    run()
