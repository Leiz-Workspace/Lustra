# coding=UTF-8
"""
Lustra: extension manifest (manifest.json) model and validation
"""
#########################################
#
#

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import asdict, dataclass, field

from .defines import UTF8

__all__ = ('MANIFEST_API_VERSION', 'MANIFEST_FILE_NAME', 'ExtensionManifest', 'ManifestError', 'TagSyntax')

MANIFEST_API_VERSION = 1
MANIFEST_FILE_NAME = 'manifest.json'
KNOWN_MEDIA = ('image', 'video', 'audio')

re_ext_id = re.compile(r'^[a-z][a-z\d_-]{1,31}$', re.ASCII)
re_abbr = re.compile(r'^[a-z][a-z\d]{1,3}$', re.ASCII)
re_version = re.compile(r'^\d+\.\d+\.\d+(?:[-+][\w.]+)?$', re.ASCII)
re_entry = re.compile(r'^(?:[\w.]+|[\w-]+\.py):[A-Za-z_]\w*$', re.ASCII)


class ManifestError(ValueError):
    pass


@dataclass
class TagSyntax:
    """Tag syntax / limits of a website. Values are only required for modules unknown to the core"""
    concat: str = '+'
    idSep: str = ':'
    maxTagsStringLen: int = 3300
    maxTags: int = 0
    '''0 = unlimited'''
    maxTagsSeparate: bool = False
    maxWildcards: int = 0
    freeMetaPrefixes: list[str] = field(default_factory=list)
    localPositiveOverflow: bool = False
    metaCount: str = r':(?:(?:[<>]=?|=)?[a-z\d\-_]+?|[a-z\d_]+:[a-z\d_]+?)'
    sort: str = r'sort(?::[^:]+?){1,2}'
    fav: str = r'favorited_by:\d+?'
    pool: str = r'pool:\d+?'

    @staticmethod
    def from_dict(d: dict | None) -> TagSyntax:
        syn = TagSyntax()
        if d is not None and not isinstance(d, dict):
            raise ManifestError('syntax must be an object')
        for k, v in (d or {}).items():
            if not hasattr(syn, k):
                raise ManifestError(f'unknown syntax field \'{k}\'')
            default = getattr(syn, k)
            if isinstance(default, bool):
                ok = isinstance(v, bool)
            elif isinstance(default, int):
                ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
            elif isinstance(default, list):
                ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
            else:
                ok = isinstance(v, str)
            if not ok:
                raise ManifestError(f'syntax.{k} has invalid type')
            setattr(syn, k, v)
        for rk in ('metaCount', 'sort', 'fav', 'pool'):
            try:
                re.compile(getattr(syn, rk))
            except re.error as err:
                raise ManifestError(f'syntax.{rk} is not a valid regex: {err}')
        if syn.concat not in ('+', ' ') or syn.idSep not in (':', '='):
            raise ManifestError('syntax.concat must be \'+\' or \' \', syntax.idSep must be \':\' or \'=\'')
        return syn


@dataclass
class ExtensionManifest:
    id: str
    name: str
    abbr: str
    version: str
    entry: str
    apiVersion: int = MANIFEST_API_VERSION
    kind: str = 'downloader'
    default: bool = False
    site: str = ''
    media: list[str] = field(default_factory=lambda: ['image', 'video'])
    tagList: str = ''
    description: str = ''
    homepage: str = ''
    auth: dict = field(default_factory=dict)
    syntax: TagSyntax = field(default_factory=TagSyntax)
    sha256: str = ''
    genderRules: str = ''
    '''optional gender ruleset file inside the extension folder (lustra/gender_rules/*.json format)'''
    safetyTags: list[str] = field(default_factory=list)
    '''optional extra tags for the safety blacklist (can only add)'''
    # runtime (not part of the file)
    generator: dict = field(default_factory=dict)
    '''origin of a generated extension, from the '$generator' key (lustra.extgen / lustra.aigen): shown as a badge (rule R13)'''
    path: pathlib.Path | None = None
    source: str = ''
    '''builtin | catalog | user'''

    @staticmethod
    def load(manifest_path: pathlib.Path, source: str) -> ExtensionManifest:
        try:
            raw = json.loads(manifest_path.read_text(encoding=UTF8))
        except Exception as err:
            raise ManifestError(f'{manifest_path}: unable to read manifest: {err}')
        return ExtensionManifest.from_dict(raw, manifest_path.parent, source)

    @staticmethod
    def from_dict(raw: dict, path: pathlib.Path | None, source: str) -> ExtensionManifest:
        try:
            return ExtensionManifest._from_dict(raw, path, source)
        except ManifestError:
            raise
        except Exception as err:  # any malformed value must surface as ManifestError (never crash the loader)
            raise ManifestError(f'malformed manifest: {err}')

    @staticmethod
    def _check_types(raw: dict) -> None:
        for k in ('id', 'name', 'abbr', 'version', 'entry', 'kind', 'site', 'tagList', 'description', 'homepage', 'sha256', 'genderRules'):
            if k in raw and not isinstance(raw[k], str):
                raise ManifestError(f'\'{k}\' must be a string')
        if 'apiVersion' in raw and (not isinstance(raw['apiVersion'], int) or isinstance(raw['apiVersion'], bool)):
            raise ManifestError('\'apiVersion\' must be an integer')
        if 'default' in raw and not isinstance(raw['default'], bool):
            raise ManifestError('\'default\' must be a boolean')
        for k in ('media', 'safetyTags'):
            if k in raw and (not isinstance(raw[k], list) or not all(isinstance(x, str) for x in raw[k])):
                raise ManifestError(f'\'{k}\' must be a list of strings')
        if 'auth' in raw and not isinstance(raw['auth'], dict):
            raise ManifestError('\'auth\' must be an object')

    @staticmethod
    def _from_dict(raw: dict, path: pathlib.Path | None, source: str) -> ExtensionManifest:
        if not isinstance(raw, dict):
            raise ManifestError('manifest must be a json object')
        ExtensionManifest._check_types(raw)
        required = ('id', 'name', 'abbr', 'version', 'entry')
        if missing := [k for k in required if not raw.get(k)]:
            raise ManifestError(f'missing required field(s): {", ".join(missing)}')
        known = set(ExtensionManifest.__dataclass_fields__) - {'path', 'source', 'generator'}
        if unknown := [k for k in raw if k not in known and not k.startswith('$')]:
            raise ManifestError(f'unknown field(s): {", ".join(unknown)}')
        m = ExtensionManifest(**{k: v for k, v in raw.items() if k in known and k != 'syntax'})
        m.syntax = TagSyntax.from_dict(raw.get('syntax'))
        gen = raw.get('$generator')
        if isinstance(gen, dict):  # only short plain strings of known keys: it is displayed, never interpreted
            m.generator = {k: str(gen[k])[:80] for k in ('tool', 'mode', 'model', 'engine', 'createdAt', 'session')
                           if isinstance(gen.get(k), (str, int, float)) and str(gen.get(k)).strip()}
        m.path, m.source = path, source
        m.validate()
        return m

    def validate(self) -> None:
        if not re_ext_id.fullmatch(self.id):
            raise ManifestError(f'invalid id \'{self.id}\'')
        if not re_abbr.fullmatch(self.abbr):
            raise ManifestError(f'invalid abbr \'{self.abbr}\' (2-4 lowercase letters/digits)')
        if not re_version.fullmatch(self.version):
            raise ManifestError(f'invalid version \'{self.version}\' (semver expected)')
        if not re_entry.fullmatch(self.entry):
            raise ManifestError(f'invalid entry \'{self.entry}\' (expected \'downloader.py:ClassName\' or \'package.module:ClassName\')')
        if self.apiVersion != MANIFEST_API_VERSION:
            raise ManifestError(f'unsupported apiVersion {self.apiVersion} (supported: {MANIFEST_API_VERSION})')
        if self.kind != 'downloader':
            raise ManifestError(f'unsupported kind \'{self.kind}\'')
        if bad := [m for m in self.media if m not in KNOWN_MEDIA]:
            raise ManifestError(f'unknown media type(s): {", ".join(bad)}')

    @property
    def is_file_entry(self) -> bool:
        return self.entry.split(':', 1)[0].endswith('.py')

    def to_public_dict(self) -> dict:
        d = asdict(self)
        d['path'] = self.path.as_posix() if self.path else ''
        return d

#
#
#########################################
