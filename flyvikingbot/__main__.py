import logging
import sys

from .bot import FlyVikingBot
from .config import ConfigError, load_settings
from .logging_setup import setup_logging


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    setup_logging(settings.data_dir)
    bot = FlyVikingBot(settings)
    # log_handler=None: keep our own logging configuration
    bot.run(settings.discord_token, log_handler=None, root_logger=False)
    logging.shutdown()


if __name__ == "__main__":
    main()
