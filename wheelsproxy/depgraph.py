import io
import itertools
import collections
import textwrap

import furl

import attr

from django.core.exceptions import ObjectDoesNotExist

from pkg_resources import Requirement, parse_version
from packaging.specifiers import SpecifierSet
from packaging.requirements import Requirement as BaseRequirement

from . import utils


DEFAULT_UNSAFE_PACKAGES = frozenset([
    'setuptools',
])


class CompilationFailed(Exception):
    pass


@attr.s
class UnsatisfiedDependency(CompilationFailed):
    requirement = attr.ib()
    versions = attr.ib(convert=tuple)


@attr.s
class IncompatibleRequirements(CompilationFailed):
    requirements = attr.ib(convert=tuple)


def merge_requirements(*reqs):
    assert reqs

    key = reqs[0].key
    extras = set()
    specifier = SpecifierSet()
    url = None

    for req in reqs:
        assert req.key == key
        # No markers shall be set at this point anymore
        assert not req.marker
        if req.url:
            assert url is None or url == req.url
            url = req.url
            spec = str(furl.furl(url).fragment.args['egg'])
            req = Requirement.parse(spec)
            assert req.key == key

        specifier &= req.specifier
        extras |= set(req.extras)

    req = BaseRequirement(key)
    req.extras = extras
    req.specifier = specifier
    req.marker = None
    req.url = None

    req = Requirement.parse(str(req))

    if url:
        key, version = str(furl.furl(url).fragment.args['egg']).split('==')
        if parse_version(version) not in req:
            raise IncompatibleRequirements(reqs)
        req = Requirement.parse('{}@{}'.format(key, url))

    return req


def find_best_release(indexes, req):
    versions = []

    for index in indexes:
        try:
            package = index.get_package(req.key, create=False)
        except ObjectDoesNotExist:
            continue
        versions.extend(package.get_versions())

    for version, release in sorted(versions, reverse=True, key=lambda v: v[0]):
        # TODO .is_prerelease is too naive, if req is ==
        if not version.is_prerelease and version in req:
            return release
    else:
        raise UnsatisfiedDependency(req, [v[0] for v in sorted(versions)])


@attr.s(slots=True)
class DependencyNode(object):
    requirement = attr.ib()
    build = attr.ib(default=None)
    declared = attr.ib(default=False)
    required_by = attr.ib(default=attr.Factory(list))

    @property
    def package_name(self):
        return self.requirement.key

    def merge_requirements(self, req, *, required_by, clear_build=True):
        merged_requirement = merge_requirements(self.requirement, req)
        if required_by:
            self.required_by.append(required_by)
        else:
            self.declared = True

        if merged_requirement == self.requirement:
            # The requirements did not change, there is no need to
            # clear the currently selected build.
            return False

        self.requirement = merged_requirement

        if clear_build:
            self.build = None

        return True

    def is_url(self):
        return bool(self.build.is_external())

    def formatted_requirement(self):
        assert self.build

        if self.build.is_external():
            return '{}=={}'.format(
                self.build.package_name,
                self.build.version,
            )
        else:
            return self.build.release.requirement


@attr.s
class DependencyGraph(object):
    indexes = attr.ib()
    platform = attr.ib()

    _nodes = attr.ib(init=False, default=attr.Factory(collections.OrderedDict))
    _log = attr.ib(init=False, default=attr.Factory(io.StringIO))

    def add_requirement(self, req):
        if req.marker:
            if not req.marker.evaluate(self.platform.environment):
                return
            else:
                req.marker = None
                req = Requirement.parse(str(req))
        self.update_requirement(req, required_by=None)

    def update_requirement(self, req, *, required_by):
        key = utils.normalize_package_name(req.key)
        if key in self._nodes:
            return self._nodes[key].merge_requirements(
                req, required_by=required_by, clear_build=True)
        else:
            if required_by:
                self._nodes[key] = DependencyNode(req, required_by=[
                    required_by,
                ])
            else:
                self._nodes[key] = DependencyNode(req, declared=True)
            return True

    def __iter__(self):
        return iter(self._nodes.values())

    def __len__(self):
        return len(self._nodes)

    def __contains__(self, req):
        if isinstance(req, str):
            req = Requirement.parse(req)
        if not isinstance(req, Requirement):
            return False
        key = utils.normalize_package_name(req.key)
        try:
            node = self._nodes[key]
        except KeyError:
            return False

        if not node.build:
            # No build was selected yet
            return True

        return node.build.release.parsed_version in req

    def _remove_node(self, node):
        key = utils.normalize_package_name(node.package_name)
        del self._nodes[key]

    def _add_requirements(self, node):
        if not node.build.is_built():
            # print('Building', node.build)
            node.build.rebuild()

        tainted = False

        for req in node.build.iter_requirements(node.requirement.extras):
            if self.update_requirement(req, required_by=node.build):
                self._log.write('  adding {}\n    from {}\n'.format(
                    req, node.formatted_requirement()))
                tainted = True

        return tainted

    def _contains_build(self, build):
        if build.is_external():
            return Requirement.parse('{}@{}'.format(
                build.package_name,
                build.external_url,
            ))
        else:
            return build.release.requirement in self

    def _remove_round(self):
        removed = False

        for node in list(self._nodes.values()):
            required_by = [
                build for build in node.required_by
                if self._contains_build(build)
            ]
            if not node.declared and not required_by:
                self._log.write('removing {}\n'.format(node))
                self._remove_node(node)
                removed = True
            elif len(required_by) != len(node.required_by):
                node.required_by = required_by

        return removed

    def _compile_round(self):
        tainted = False

        self._log.write('Adding new dependencies:\n')

        for node in list(self._nodes.values()):
            if node.build is not None:
                continue
            if node.requirement.url:
                node.build = self.platform.get_external_build(
                    node.requirement.url,
                )
            else:
                try:
                    node.build = find_best_release(
                        self.indexes,
                        node.requirement,
                    ).get_build(self.platform)
                except UnsatisfiedDependency as e:
                    self._log.write(
                        'Could not find a version that matches {}\n'
                        .format(e.requirement)
                    )
                    self._log.write(textwrap.fill('Tried: {}\n'.format(
                        ', '.join([str(v) for v in e.versions])
                    )))
                    raise
                except IncompatibleRequirements as e:
                    self._log.write(
                        'Cannot merge incompatible requirements:\n'
                    )
                    self._log.write(
                        '\n'.join([str(v) for v in e.requirements])
                    )
                    raise
            tainted |= self._add_requirements(node)

        return tainted

    def remove_orphaned_requirements(self):
        tainted = False

        for round in itertools.count(1):
            if not self._remove_round():
                break
            tainted = True

        return tainted

    def compile(self, requirements):
        self._nodes = collections.OrderedDict()
        self._log = io.StringIO()

        self._log.write('Using indexes:\n')
        for index in self.indexes:
            self._log.write(' - {}: {}\n'.format(index.slug, index.url))
        self._log.write('\n')

        for req in utils.parse_requirements(requirements):
            self.add_requirement(req)

        for round in itertools.count(1):
            self._log.write('ROUND {}\n'.format(round))

            self._log.write('Current constraints:\n')
            for node in sorted(self, key=lambda n: n.package_name.lower()):
                self._log.write('  {}\n'.format(node.requirement))
            self._log.write('\n')

            tainted = False
            tainted |= self._compile_round()
            tainted |= self.remove_orphaned_requirements()

            if not tainted:
                self._log.write(
                    '--------------------------------------------\n'
                    'Result of round {}: stable, done\n\n'
                    .format(round)
                )
                break
            else:
                self._log.write(
                    '--------------------------------------------\n'
                    'Result of round {}: not stable\n\n'
                    .format(round)
                )

        return self._log.getvalue()

    def get_last_log(self):
        return self._log.getvalue()


@attr.s
class GraphFormatter(object):
    show_parents = attr.ib(default=27)
    unsafe_packages = attr.ib(
        convert=frozenset,
        default=DEFAULT_UNSAFE_PACKAGES,
    )
    header_comment = attr.ib(default=False)

    def write_comment(self, fh, comment):
        wrapper = textwrap.TextWrapper(
            initial_indent='# ',
            subsequent_indent='# ',
        )
        fh.write(wrapper.fill(comment))
        fh.write('\n')

    def write_requirement(self, fh, node, *, commented=False):
        line = ''
        if commented:
            line += '# '
        if node.is_url():
            line += node.build.external_url
        else:
            line += '{}=={}'.format(
                node.build.release.package.name.lower(),
                node.build.release.version,
            )
        if self.show_parents and not node.declared and node.required_by:
            line = line.ljust(self.show_parents - 3)
            line += '   # via {}'.format(', '.join(sorted(set(
                build.package_name
                for build in node.required_by
            ))))
        fh.write(line)
        fh.write('\n')

    def write(self, fh, graph):
        unsafe_nodes = []

        if self.header_comment:
            fh.write(self.header_comment)

        wrote_external_dep = False
        for node in graph:
            if not node.is_url():
                continue
            if not wrote_external_dep:
                fh.write('\n')
                wrote_external_dep = True
            self.write_requirement(fh, node)

        for node in sorted(graph, key=lambda n: n.package_name.lower()):
            if node.package_name in self.unsafe_packages:
                unsafe_nodes.append(node)
                continue
            if node.is_url():
                continue
            if wrote_external_dep:
                wrote_external_dep = False
                fh.write('\n')
            self.write_requirement(fh, node)

        if unsafe_nodes:
            fh.write('\n')

            self.write_comment(fh, (
                'The following packages are commented out because they '
                'are considered to be unsafe in a requirements file:'
            ))
            for node in unsafe_nodes:
                self.write_requirement(fh, node, commented=True)

    def format(self, graph):
        fh = io.StringIO()
        self.write(fh, graph)
        return fh.getvalue()
