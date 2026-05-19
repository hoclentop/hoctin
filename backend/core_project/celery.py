import os
from celery import Celery

# Thiết lập settings mặc định cho django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core_project.settings')

app = Celery('core_project')

# Load cấu hình từ settings.py với prefix CELERY_
app.config_from_object('django.conf:settings', namespace='CELERY')

# Tự động tìm tasks trong các app
app.autodiscover_tasks()

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
