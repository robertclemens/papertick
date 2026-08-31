from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

celery = Celery("papertick", broker=get_settings().redis_url)

celery.conf.update(
    task_ignore_result=True,
    timezone="UTC",
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "run-recurring-investments": {
            "task": "app.workers.tasks.run_recurring_investments",
            "schedule": 60.0,
        },
        "run-scheduled-orders": {
            "task": "app.workers.tasks.run_scheduled_orders",
            "schedule": 60.0,
        },
        "run-limit-orders": {
            "task": "app.workers.tasks.run_limit_orders",
            "schedule": 60.0,
        },
        "expire-orders": {
            "task": "app.workers.tasks.expire_orders",
            "schedule": 300.0,
        },
        "accrue-settlement-dividends": {
            "task": "app.workers.tasks.accrue_settlement_dividends",
            # after midnight ET: accrues the day just ended and pays the
            # month-end distribution
            "schedule": crontab(hour=5, minute=30),
        },
        "purge-expired-scenarios": {
            "task": "app.workers.tasks.purge_expired_scenarios",
            "schedule": crontab(hour=4, minute=30),
        },
        "reconcile-dividends": {
            "task": "app.workers.tasks.reconcile_dividends",
            "schedule": 6 * 3600.0,
        },
        "process-option-expirations": {
            "task": "app.workers.tasks.process_option_expirations",
            "schedule": 600.0,  # settles expired contracts after the close
        },
        "generate-statements": {
            "task": "app.workers.tasks.generate_statements",
            "schedule": crontab(hour=6, minute=15, day_of_month=1),
        },
        "ensure-irs-limits": {
            "task": "app.workers.tasks.ensure_irs_limits",
            "schedule": crontab(hour=5, minute=0),
        },
    },
)

celery.autodiscover_tasks(["app.workers"])
from app.workers import tasks  # noqa: E402,F401  (register tasks)
