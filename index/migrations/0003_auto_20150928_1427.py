# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('index', '0002_auto_20150928_1338'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='build',
            unique_together=set([('release', 'platform')]),
        ),
    ]
