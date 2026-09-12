import logging
from app.config import Settings


class SecretFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [value for value in secrets if value and value != 'hogehoge']

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, '[REDACTED]')
        record.msg, record.args = message, ()
        return True


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    handler.addFilter(SecretFilter([settings.openai_api_key.get_secret_value(),
                                   settings.discord_webhook_url.get_secret_value()]))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    for name in ('httpx', 'httpcore', 'openai'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
