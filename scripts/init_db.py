#!/usr/bin/env python3
"""Create the 'liquidation' database and all tables."""

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import get_config
from collectors.db import create_database, create_tables

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def main() -> None:
    cfg = get_config()
    log.info("Connecting to PostgreSQL at %s:%s", cfg.db_host, cfg.db_port)

    create_database(cfg)
    create_tables(cfg)

    log.info("Database '%s' is ready with all tables.", cfg.db_name)


if __name__ == "__main__":
    main()
