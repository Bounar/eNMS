from os import environ
from typing import Dict, Type


class Config(object):
    CACHE_TYPE = "simple"
    MAIL_SERVER = environ.get("MAIL_SERVER", "smtp.googlemail.com")
    MAIL_PORT = int(environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS = int(environ.get("MAIL_USE_TLS", True))
    MAIL_USERNAME = environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = environ.get("MAIL_PASSWORD")
    MAIL_SENDER = environ.get("MAIL_SENDER", "enms@enms.fr")
    MAIL_RECIPIENTS = environ.get("MAIL_RECIPIENTS", "")


class DefaultConfig(Config):
    DEBUG = True
    SECRET_KEY = environ.get("ENMS_SECRET_KEY", "get-a-real-key")
    MAIL_DEBUG = 1
    DEBUG_TB_ENABLED = False


class DevelopConfig(DefaultConfig):
    DEVELOP = True
    DEBUG_TB_ENABLED = True
    DEBUG_TB_PROFILER_ENABLED = True
    DEBUG_TB_INTERCEPT_REDIRECTS = False


class ProductionConfig(Config):
    DEBUG = False
    SECRET_KEY = environ.get("ENMS_SECRET_KEY")
    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_DURATION = 3600


class TestConfig(DefaultConfig):
    CACHE_TYPE = "null"
    WTF_CSRF_ENABLED = False


config_dict: Dict[str, Type[Config]] = {
    "Default": DefaultConfig,
    "Develop": DevelopConfig,
    "Production": ProductionConfig,
    "Test": TestConfig,
}
