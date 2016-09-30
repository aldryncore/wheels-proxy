from celery import Celery

from django.conf import settings

from raven.contrib.celery import register_signal, register_logger_signal


app = Celery('wheelsproxy')
app.config_from_object('django.conf:settings')
app.autodiscover_tasks(lambda: settings.INSTALLED_APPS)

raven_config = getattr(settings, 'RAVEN_CONFIG', {})
sentry_dsn = raven_config.get('dsn', None)
if sentry_dsn:
    from raven.contrib.django.models import client
    register_logger_signal(client)
    register_signal(client)
