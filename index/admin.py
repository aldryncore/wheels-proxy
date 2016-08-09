import json

from django.db.models import Count
from django.contrib import admin
from django.contrib.humanize.templatetags.humanize import intcomma
from django.utils import html
from django.utils.translation import ugettext_lazy as _
from django.template.defaultfilters import filesizeformat

from . import models, adminutils, tasks
from .adminutils import (
    simple_code_block,
    admin_detail_link,
    queryset_action,
    linked_inline,
    linked_relation,
)


@admin.register(models.Platform)
class PlatformAdmin(adminutils.ModelAdmin):
    pass


@admin.register(models.BackingIndex)
class BackingIndexAdmin(adminutils.ModelAdmin):
    list_display = (
        'slug',
        'formatted_url',
        'package_count',
        'release_count',
        'last_update_serial',
    )

    readonly_fields = (
        'formatted_last_upstream_serial',
    )

    actions = (
        'sync_index_action',
    )

    change_actions = (
        'sync_index_action',
    )

    @queryset_action
    def sync_index_action(self, request, queryset):
        for index_pk in queryset.values_list('pk', flat=True):
            tasks.sync_index.delay(index_pk)
    sync_index_action.label = _('Sync')
    sync_index_action.short_description = _('Sync the selected indexes')

    def get_queryset(self, request):
        return (
            super(BackingIndexAdmin, self)
            .get_queryset(request)
            .annotate(package_count=Count('package', distinct=True))
            .annotate(release_count=Count('package__release', distinct=True))
        )

    def release_count(self, instance):
        return intcomma(instance.release_count)
    release_count.short_description = _('Releases')
    release_count.admin_order_field = 'release_count'

    def package_count(self, instance):
        return intcomma(instance.package_count)
    package_count.short_description = _('Packages')
    package_count.admin_order_field = 'package_count'

    def formatted_url(self, instance):
        return html.format_html('<a href="{0}">{0}</a>', instance.url)
    formatted_url.short_description = _('URL')
    formatted_url.admin_order_field = 'url'

    def formatted_last_upstream_serial(self, instance):
        if not instance.last_update_serial:
            return '-'
        upstream_serial = instance.last_upstream_serial()
        return '{} ({} events to sync)'.format(
            upstream_serial,
            upstream_serial - instance.last_update_serial,
        )
    formatted_last_upstream_serial.short_description = 'last upstream serial'


class ReleaseInline(admin.TabularInline):
    fields = (
        'formatted_version',
    )
    readonly_fields = (
        'formatted_version',
    )
    model = models.Release
    extra = 0

    formatted_version = linked_inline('version')


@admin.register(models.Package)
class PackageAdmin(adminutils.ModelAdmin):
    list_display = (
        'slug',
        'index_name',
    )

    search_fields = ('name',)

    list_filter = (
        'index',
    )

    inlines = (
        ReleaseInline,
    )

    actions = (
        'expire_cache_action',
    )

    change_actions = (
        'expire_cache_action',
    )

    index_name = linked_relation('index')

    @queryset_action
    def expire_cache_action(self, request, queryset):
        for package in queryset.iterator():
            package.expire_cache()
    expire_cache_action.label = _('Invalidate cache')
    expire_cache_action.short_description = _(
        'Invalidate cache for the selected packages (all platforms)')


class BuildInline(admin.TabularInline):
    fields = (
        'formatted_platform',
        'is_built',
        'formatted_filesize',
    )
    readonly_fields = (
        'formatted_platform',
        'is_built',
        'formatted_filesize',
    )
    model = models.Build
    extra = 0

    formatted_platform = linked_inline('platform')

    def formatted_filesize(self, instance):
        if instance.is_built():
            return filesizeformat(instance.filesize)
        else:
            return 'n/d'
    formatted_filesize.short_description = 'wheel size'


@admin.register(models.Release)
class ReleaseAdmin(adminutils.ModelAdmin):
    list_display = (
        'version',
        'package_name',
        'index_name',
    )

    raw_id_fields = (
        'package',
    )

    search_fields = (
        'package__name',
    )

    inlines = (
        BuildInline,
    )

    readonly_fields = (
        'md5_digest',
    )

    list_filter = (
        'package__index',
    )

    package_name = linked_relation('package')

    index_name = linked_relation('package__index', _('index'))

    def get_queryset(self, request):
        return (
            super(ReleaseAdmin, self)
            .get_queryset(request)
            .select_related('package__index')
        )

    def index(self, instance):
        return instance.package.index
    index.admin_order_field = 'package__index'


class BuildStatusListFilter(admin.SimpleListFilter):
    title = 'build status'
    parameter_name = 'is_built'

    def lookups(self, request, model_admin):
        return (
            ('no', 'Not yet built'),
            ('yes', 'Already built'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'no':
            return queryset.filter(build='')
        if self.value() == 'yes':
            return queryset.exclude(build='')


@admin.register(models.Build)
class BuildAdmin(adminutils.ModelAdmin):
    list_display = (
        'version',
        'package_name',
        'index_name',
        'platform_name',
        'is_built',
    )

    list_filter = (
        'platform',
        'release__package__index',
        BuildStatusListFilter,
    )

    readonly_fields = (
        'formatted_filesize',
        'md5_digest',
        'build_timestamp',
        'formatted_build_duration',
        'formatted_requirements',
        'formatted_metadata',
        'formatted_build_log',
    )

    search_fields = (
        'release__package__name',
    )

    raw_id_fields = (
        'release',
    )

    actions = (
        'rebuild_action',
    )
    change_actions = (
        'rebuild_action',
    )

    def get_queryset(self, request):
        return (
            super(BuildAdmin, self)
            .get_queryset(request)
            .select_related('release__package__index')
            .select_related('platform')
        )

    @queryset_action
    def rebuild_action(self, request, queryset):
        for build_pk in queryset.values_list('pk', flat=True):
            tasks.build.delay(build_pk, force=True)
    rebuild_action.label = _('Rebuild')
    rebuild_action.short_description = _(
        'Trigger a rebuild for the selected builds')

    def version(self, build):
        return build.release.version

    package_name = linked_relation('release__package', _('package'))

    index_name = linked_relation('release__package__index', _('index'))

    platform_name = linked_relation('platform')

    def is_built(self, build):
        return bool(build.build)
    is_built.boolean = True
    is_built.admin_order_field = 'build'

    def formatted_requirements(self, instance):
        reqs = instance.requirements
        if reqs is not None and reqs:
            reqs = (str(r) for r in reqs)
            reqs = sorted(reqs, key=lambda k: k.lower())
            return simple_code_block('\n'.join(reqs))
        elif reqs is not None:
            return _('No dependencies')
        else:
            return '-'
    formatted_requirements.short_description = _('requirements')

    def formatted_metadata(self, instance):
        return simple_code_block(
            json.dumps(instance.metadata, indent=4)
        )
    formatted_metadata.short_description = _('metadata')

    def formatted_build_log(self, instance):
        if not instance.build:
            return '-'
        return simple_code_block(instance.build_log)
    formatted_build_log.short_description = _('build log')

    def formatted_build_duration(self, instance):
        return _('{} seconds').format(instance.build_duration)
    formatted_build_duration.short_description = _('build duration')

    def formatted_filesize(self, instance):
        if instance.is_built():
            return filesizeformat(instance.filesize)
        else:
            return 'n/d'
    formatted_filesize.short_description = _('wheel size')
