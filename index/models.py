import os
import logging
import re

import six
from pkg_resources import parse_version

from django.db import models
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from django.utils.functional import cached_property
from django.utils.module_loading import import_string
from django.contrib.postgres.fields import JSONField

from extended_choices import Choices

from . import storage, tasks, builder, utils, client


log = logging.getLogger(__name__)


INDEX_BACKENDS = Choices(
    ('PYPI', 'index.client.PyPIClient', _('PyPI')),
    ('DEVPI', 'index.client.DevPIClient', _('DevPI')),
)


def normalize_package_name(package_name):
    return re.sub(r'(\.|-|_)+', '-', package_name.lower())


class Platform(models.Model):
    DOCKER = 'docker'
    PLATFORM_CHOICES = [
        (DOCKER, _('Docker'))
    ]

    slug = models.SlugField(unique=True)
    type = models.CharField(max_length=16, choices=PLATFORM_CHOICES)
    spec = JSONField(default={})

    def __str__(self):
        return self.slug

    def get_builder(self):
        # TODO: If we need to support more platform types here (e.g. use VMs
        # for platforms not supported by docker: OS X, Windows, ...)
        assert self.type == self.DOCKER
        return builder.DockerBuilder(self.spec)


class BackingIndex(models.Model):
    slug = models.SlugField(unique=True)
    url = models.URLField(verbose_name=_('URL'))
    last_update_serial = models.BigIntegerField(null=True, blank=True)
    backend = models.CharField(
        max_length=255,
        choices=INDEX_BACKENDS,
        default=INDEX_BACKENDS.PYPI,
    )

    class Meta:
        verbose_name_plural = _('backing indexes')

    def __str__(self):
        return self.slug

    def get_client(self):
        Client = import_string(self.backend)
        return Client(self.url)

    client = cached_property(get_client)

    def get_package(self, package_name):
        normalized_package_name = normalize_package_name(package_name)
        package, created = Package.objects.get_or_create(
            index=self,
            slug=normalized_package_name,
            defaults={'name': package_name},
        )
        return package

    def itersync(self):
        serial = self.last_update_serial
        packages_to_update = self.client.iter_updated_packages(serial)
        for package_name, serial in packages_to_update:
            if package_name:
                if not self.import_package(package_name):
                    # Nothing imported: remove the package
                    Package.objects.filter(
                        index=self,
                        slug=normalize_package_name(package_name),
                    ).delete()
            if serial > self.last_update_serial:
                self.last_update_serial = serial
                yield self.last_update_serial
        self.save(update_fields=['last_update_serial'])

    def sync(self):
        for i in self.itersync():
            pass

    def import_package(self, package_name):
        # log.info('importing {} from {}'.format(package_name, self.url))
        try:
            versions = self.client.get_package_releases(package_name)
        except client.PackageNotFound:
            log.debug('package {} not found on {}'
                      .format(package_name, self.url))
            return
        if not versions:
            log.debug('no versions found for package {} on {}'
                      .format(package_name, self.url))
            return
        package = self.get_package(package_name)
        release_ids = []
        for version, releases in six.iteritems(versions):
            release_details = package.get_best_release(releases)
            if not release_details:
                continue
            release = package.get_release(version, release_details)
            release_ids.append(release.pk)
        if release_ids:
            # Remove outdated releases
            package.release_set.exclude(pk__in=release_ids).delete()
        package.expire_cache()
        return package.pk if release_ids else None

    def expire_cache(self, platform=None):
        if platform:
            platforms = [platform]
        else:
            platforms = Platform.objects.all()
        for slug in self.package_set.values_list('slug').all():
            for platform in platforms:
                for namespace in ('links',):
                    key = Package.get_cache_key(
                        namespace,
                        self.slug,
                        platform.slug,
                        slug,
                    )
                    if cache.has_key(key):
                        cache.delete(key)


class Package(models.Model):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    index = models.ForeignKey(BackingIndex)

    class Meta:
        unique_together = ('slug', 'index')
        ordering = ('slug', )

    def __str__(self):
        return self.slug

    def get_best_release(self, releases):
        for release in releases:
            if release.type == 'sdist':
                return release
        for release in releases:
            if release.type == 'bdist_wheel':
                if release.url.endswith('-py2.py3-none-any.whl'):
                    return release

    def get_release(self, version, release=None):
        instance, created = Release.objects.get_or_create(
            package=self, version=version)
        if created:
            if not release:
                releases = self.index.client.get_version_releases(
                    self.slug,
                    version,
                )
                release = self.get_best_release(releases)

            instance.url = release.url
            instance.md5_digest = release.md5_digest
            assert instance.url
            assert instance.md5_digest
            instance.save(update_fields=['url', 'md5_digest'])
        elif release:
            instance.url = release.url
            instance.md5_digest = release.md5_digest
            instance.save(update_fields=['url', 'md5_digest'])
        return instance

    @classmethod
    def get_cache_key(cls, namespace, index_slug, platform_slug, package_name):
        return '{}-index:{}-platform:{}-package:{}'.format(
            namespace, index_slug, platform_slug, package_name)

    def expire_cache(self, platform=None):
        if platform:
            platforms = [platform]
        else:
            platforms = Platform.objects.all()
        for platform in platforms:
            for namespace in ('links',):
                key = self.get_cache_key(
                    namespace,
                    self.index.slug,
                    platform.slug,
                    self.slug,
                )
                if cache.has_key(key):
                    cache.delete(key)

    def get_builds(self, platform, check=True):
        releases = (Release.objects
                    .filter(package=self)
                    .only('pk')
                    .all())
        builds_qs = (Build.objects
                     .filter(release__in=releases, platform=platform)
                     .order_by('-release__version')
                     .all())

        if check and (len(builds_qs) != len(releases)):
            for r in releases:
                r.get_build(platform)

        return builds_qs

    def get_versions(self):
        return sorted([
            (rel.parsed_version, rel)
            for rel in self.release_set.all()
        ], reverse=True)


class Release(models.Model):
    package = models.ForeignKey(Package)
    version = models.CharField(max_length=200)
    url = models.URLField(blank=True, max_length=255, default='')
    md5_digest = models.CharField(
        verbose_name=_('MD5 digest'),
        max_length=32,
        default='',
        blank=True,
        editable=False,
    )
    last_update = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('package', 'version')
        ordering = ('package', 'version')

    def __str__(self):
        return '{}-{}'.format(self.package.slug, self.version)

    def get_build(self, platform):
        build, created = Build.objects.get_or_create(
            release=self, platform=platform)
        return build

    @cached_property
    def parsed_version(self):
        return parse_version(self.version)


def upload_build_to(self, filename):
    return '{index}/{platform}/{package}/{version}/{filename}'.format(
        index=self.release.package.index.slug,
        package=self.release.package.slug,
        version=self.release.version,
        platform=self.platform.slug,
        filename=filename,
    )


class BuildsManager(models.Manager):
    use_for_related_fields = True

    def get_queryset(self, *args, **kwargs):
        return (super(BuildsManager, self)
                .get_queryset(*args, **kwargs)
                .defer('build_log')
                .select_related('release__package__index'))


class Build(models.Model):
    release = models.ForeignKey(Release)
    platform = models.ForeignKey(Platform)
    md5_digest = models.CharField(
        verbose_name=_('MD5 digest'),
        max_length=32,
        default='',
        blank=True,
        editable=False,
    )
    build = models.FileField(
        storage=storage.dsn_configured_storage('BUILDS_STORAGE_DSN'),
        upload_to=upload_build_to,
        max_length=255, blank=True, null=True,
    )
    metadata = JSONField(null=True, blank=True, editable=False)
    filesize = models.PositiveIntegerField(
        blank=True, null=True,
        editable=False,
    )
    build_timestamp = models.DateTimeField(
        blank=True, null=True,
        editable=False,
    )
    build_duration = models.PositiveIntegerField(
        blank=True, null=True,
        editable=False,
    )
    build_log = models.TextField(blank=True, editable=False)

    objects = BuildsManager()

    class Meta:
        unique_together = ('release', 'platform')

    def __str__(self):
        return self.filename

    def rebuild(self):
        builder = self.platform.get_builder()
        builder(self)
        self.release.package.expire_cache(self.platform)

    def schedule_build(self, force=False):
        return tasks.build.delay(self.pk, force=force)

    def get_build_url(self, build_if_needed=False):
        if self.is_built():
            return self.build.url
        else:
            if build_if_needed:
                self.schedule_build()
            return self.original_url

    @property
    def filename(self):
        if self.is_built():
            path = self.build.name
        else:
            path = self.original_url
        return os.path.basename(path)

    @property
    def original_url(self):
        return self.release.url

    @property
    def requirements(self):
        if self.metadata:
            for requirements in self.metadata.get('run_requires', []):
                if 'extra' not in requirements:
                    return {
                        utils.parse_requirement(r)
                        for r in requirements['requires']
                    }
            else:
                return []
        else:
            return None

    def is_built(self):
        return bool(self.build)
    is_built.boolean = True

    def get_absolute_url(self):
        if self.is_built() and not settings.ALWAYS_REDIRECT_DOWNLOADS:
            # NOTE: Return the final URL directly if the build is already
            # available and ALWAYS_REDIRECT_DOWNLOADS is set to False, so that
            # we can avoid one additional request to get the redirect.
            # This prevents us from collecting stats about package activity,
            # but given the problems we're trying to solve with the proxy,
            # this is an acceptable compromise.
            return self.get_build_url()
        else:
            return reverse('index:download_build', kwargs={
                'index_slug': self.release.package.index.slug,
                'platform_slug': self.platform.slug,
                'version': self.release.version,
                'package_name': self.release.package.slug,
                'filename': self.filename,
                'build_id': self.pk,
            })

    def get_digest(self):
        if self.is_built():
            return self.md5_digest
        else:
            return self.release.md5_digest
