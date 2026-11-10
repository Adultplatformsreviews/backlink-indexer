import os
import tempfile

_keys = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "DATABASE_URL_UNPOOLED",
    "POSTGRES_URL_NON_POOLING",
    "POSTGRES_URL_NO_SSL",
    "NEON_DATABASE_URL",
    "CRON_SECRET",
    "VERCEL",
)
for _key in _keys:
    os.environ.pop(_key, None)
os.environ["APP_DATA_DIR"] = tempfile.mkdtemp(prefix="bi-test-")
os.environ["CRON_TICKS_PER_DAY"] = "1"
os.environ["QUEUE_BUDGET_SECONDS"] = "30"
