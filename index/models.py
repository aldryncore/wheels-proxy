import os

from six.moves import xmlrpc_client
import requests
import jsonfield

from django.db import models
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from django.utils.functional import cached_property

from . import storage, tasks, builder


class PackageNotFound(Exception):
    pass


class Platform(models.Model):
    DOCKER = 'docker'
    PLATFORM_CHOICES = [
        (DOCKER, _('Docker'))
    ]

    slug = models.SlugField(unique=True)
    type = models.CharField(max_length=16, choices=PLATFORM_CHOICES)
    spec = jsonfield.JSONField()

    def __str__(self):
        return self.slug

    def get_builder(self):
        # TODO: If we need to support more platform types here (e.g. use VMs
        # for platforms not supported by docker: OS X, Windows, ...)
        assert self.type == self.DOCKER
        return builder.DockerBuilder(self.spec)


class BackingIndex(models.Model):
    slug = models.SlugField(unique=True)
    url = models.URLField()
    last_update_serial = models.BigIntegerField(null=True, blank=True)

    def __str__(self):
        return self.slug

    def get_client(self):
        return xmlrpc_client.ServerProxy(self.url)

    @cached_property
    def client(self):
        return self.get_client()

    def get_package_details_url(self, package_name, version=None):
        url = '{}/{}'.format(self.url.rstrip('/'), package_name)
        if version:
            url = '{}/{}'.format(url, version)
        url = '{}/{}'.format(url, 'json')
        return url

    def get_package_details(self, package_name, version=None, session=None):
        url = self.get_package_details_url(package_name, version)
        if not session:
            session = requests
        response = session.get(url)
        if response.status_code == 404:
            raise PackageNotFound()
        if response.status_code >= 300:
            # TODO: Log content somewhere
            raise RuntimeError('Invalid response from index: {}'
                               .format(response.status_code))
        return response.json()

    def get_package(self, package_name):
        package, created = Package.objects.get_or_create(
            index=self, slug=package_name)
        return package


class Package(models.Model):
    slug = models.SlugField(max_length=200)
    index = models.ForeignKey(BackingIndex)

    class Meta:
        unique_together = ('slug', 'index')
        ordering = ('slug', )

    def __str__(self):
        return self.slug

    def get_best_release(self, releases):
        for release in releases:
            if release['packagetype'] == 'sdist':
                return release
        for release in releases:
            if release['packagetype'] == 'bdist_wheel':
                if release['filename'].endswith('-py2.py3-none-any.whl'):
                    return release

    def get_release(self, version, details=None):
        release, created = Release.objects.get_or_create(
            package=self, version=version)
        if created:
            if details:
                release.original_details = details
            else:
                info = self.index.get_package_details(self.slug, version)
                releases = info['releases'][version]
                release.original_details = self.get_best_release(releases)
            assert release.original_details
            release.save()
        elif details:
            release.original_details = details
            release.save()
        return release

    @classmethod
    def get_cache_key(cls, namespace, index_slug, platform_slug, package_name):
        return '{}-index:{}-platform:{}-package:{}'.format(
            namespace, index_slug, platform_slug, package_name)

    def expire_cache(self, platform):
        for namespace in ('links',):
            key = self.get_cache_key(
                namespace,
                self.index.slug,
                platform.slug,
                self.slug,
            )
            if cache.has_key(key):
                cache.delete(key)


class Release(models.Model):
    package = models.ForeignKey(Package)
    version = models.CharField(max_length=200)
    original_details = jsonfield.JSONField()
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


def upload_build_to(self, filename):
    return '{index}/{package}/{version}/{platform}/{filename}'.format(
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
    md5_digest = models.CharField(max_length=32, default='', blank=True)
    build = models.FileField(storage=storage.builds_storage,
                             upload_to=upload_build_to,
                             blank=True, null=True)
    filesize = models.PositiveIntegerField(blank=True, null=True)
    build_timestamp = models.DateTimeField(blank=True, null=True)
    build_duration = models.PositiveIntegerField(blank=True, null=True)
    build_log = models.TextField(blank=True)

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
        try:
            return self.release.original_details['url']
        except TypeError:
            return ''

    def is_built(self):
        return bool(self.build)

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
            return self.release.original_details['md5_digest']

    def to_pypi_dict(self):
        details = self.release.original_details
        if self.is_built():
            # TODO: Provide these values
            # details['python_version']
            details['upload_time'] = self.build_timestamp
            details['size'] = self.filesize
            details['filename'] = self.filename
            details['packagetype'] = 'bdist_wheel'
            details['md5_digest'] = self.md5_digest
        details['url'] = self.get_absolute_url()
        return details
