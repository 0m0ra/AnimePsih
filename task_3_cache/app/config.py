import os


class Config:
    DATABASE_URL = os.getenv(
        "DATABASE_URL", "postgresql+psycopg2://app:app@localhost:5433/appdb"
    )
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")
    CACHE_STRATEGY = os.getenv("CACHE_STRATEGY", "cache_aside").lower()
    WRITEBACK_FLUSH_INTERVAL = float(os.getenv("WRITEBACK_FLUSH_INTERVAL", "2.0"))
    WRITEBACK_BATCH_SIZE = int(os.getenv("WRITEBACK_BATCH_SIZE", "200"))
    CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))


VALID_STRATEGIES = {"cache_aside", "write_through", "write_back"}
