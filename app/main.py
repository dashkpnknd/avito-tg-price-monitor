from __future__ import annotations

import logging
import os
import time

from .admin_bot import AdminBot
from .monitor import Config, Monitor, MonitorError
from .projects import all_project_configs


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = Config.from_environment()
    admin_bot = AdminBot(config)
    interval = config.check_interval_minutes * 60
    next_check = 0.0
    while True:
        if time.monotonic() >= next_check:
            for project_config in all_project_configs(config):
                try:
                    summary = Monitor(project_config).run_once()
                    logging.info(
                        "Check completed: project=%s client=%s autoload=%s active_avito=%s issues=%s alerts=%s resolved=%s",
                        project_config.project_name,
                        summary.client_products,
                        summary.autoload_rows,
                        summary.active_avito_items,
                        summary.issues,
                        summary.alerts_sent,
                        summary.resolved_sent,
                    )
                except MonitorError as error:
                    logging.exception("Check failed safely for %s: %s", project_config.project_name, error)
                except Exception:  # Keep the watchdog alive for unexpected transient errors.
                    logging.exception("Unexpected check failed for %s", project_config.project_name)
            next_check = time.monotonic() + interval
        try:
            admin_bot.process_updates()
        except MonitorError as error:
            logging.exception("Telegram admin interface failed safely: %s", error)
        except Exception:
            logging.exception("Unexpected Telegram admin interface failure")
        time.sleep(2)


if __name__ == "__main__":
    main()
