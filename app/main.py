from __future__ import annotations

import logging
import os
import time

from .monitor import Config, Monitor, MonitorError


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = Config.from_environment()
    monitor = Monitor(config)
    interval = config.check_interval_minutes * 60
    while True:
        try:
            summary = monitor.run_once()
            logging.info(
                "Check completed: client=%s autoload=%s active_avito=%s issues=%s alerts=%s resolved=%s",
                summary.client_products,
                summary.autoload_rows,
                summary.active_avito_items,
                summary.issues,
                summary.alerts_sent,
                summary.resolved_sent,
            )
        except MonitorError as error:
            logging.exception("Check failed safely: %s", error)
        except Exception:  # Keep the watchdog alive for unexpected transient errors.
            logging.exception("Unexpected check failure")
        time.sleep(interval)


if __name__ == "__main__":
    main()
