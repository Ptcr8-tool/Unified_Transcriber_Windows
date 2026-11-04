from pathlib import Path
import re


def replace(path, old, new):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise RuntimeError(f'Expected one replacement in {path}, found {text.count(old)}')
    p.write_text(text.replace(old, new), encoding='utf-8', newline='\n')

assert Path('VERSION').read_text().strip() == '1.0.1'
Path('VERSION').write_text('1.0.0-a\n', encoding='ascii')
replace('_internal/core.py', "APP_VERSION = '1.0.1'", "APP_VERSION = '1.0.0-a'")
replace('_internal/installation.py',
        "_VERSION = re.compile(r'(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\Z')",
        "_VERSION = re.compile(r'(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-([a-z]))?\\Z')")
replace('_internal/installation.py',
'''def version_tuple(value: str) -> tuple[int, int, int]:
    """Compare stable release numbers numerically; reject ambiguous versions."""
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match or len(value) > 40:
        raise InstallationError('バージョン番号は X.Y.Z 形式で指定してください。')
    return tuple(int(part) for part in match.groups())''',
'''def version_tuple(value: str) -> tuple[int, int, int, int]:
    """Compare numbered releases and lettered revisions (base < -a < -b).

    Letter suffixes identify revisions of this app, not SemVer prereleases.
    A later numeric release always sorts above revisions of an earlier one.
    """
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match or len(value) > 40:
        raise InstallationError('バージョン番号は X.Y.Z または X.Y.Z-a 形式で指定してください。')
    revision = ord(match.group(4)) - ord('a') + 1 if match.group(4) else 0
    return (*map(int, match.groups()[:3]), revision)''')
replace('_internal/app_updates.py',
'''def is_check_due(mode: str, days: int, last_checked: str = '', *,''',
'''def parse_installed_version(value: str) -> tuple[int, int, int, int]:
    """Allow a local lettered revision without relaxing public release checks."""
    if not isinstance(value, str):
        raise UpdateSecurityError('バージョン表記を確認できません。')
    base, separator, suffix = value.partition('-')
    if separator and not re.fullmatch(r'[a-z]', suffix):
        raise UpdateSecurityError('バージョン表記を確認できません。')
    return (*parse_version(base), ord(suffix) - ord('a') + 1 if separator else 0)


def is_check_due(mode: str, days: int, last_checked: str = '', *,''')
replace('_internal/app_updates.py', '    current = parse_version(current_version)',
        '    current = parse_installed_version(current_version)')
replace('_internal/app_updates.py', '    candidate = parse_version(version)',
        '    candidate = (*parse_version(version), 0)')
replace('_internal/app_updates.py', '    parse_version(current_version)\n    return parse_release',
        '    parse_installed_version(current_version)\n    return parse_release')
replace('_internal/tools/build_windows.py',
        "if not re.fullmatch(r'\\d+\\.\\d+\\.\\d+', version):\n        raise ValueError('VERSION must contain a stable semantic version.')",
        "if not re.fullmatch(r'\\d+\\.\\d+\\.\\d+(?:-[a-z])?', version):\n        raise ValueError('VERSION must contain a numeric version or a single-letter revision.')")
replace('_internal/tools/native_launcher.py',
'''    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('A stable application version is required.')
    numbers = tuple(map(int, version.split('.')))''',
'''    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z])?', version):
        raise ValueError('A numeric version or a single-letter revision is required.')
    base, _, suffix = version.partition('-')
    numbers = tuple(map(int, base.split('.')))
    revision = ord(suffix) - ord('a') + 1 if suffix else 0''')
replace('_internal/tools/native_launcher.py',
        "','.join(map(str, (*numbers, 0)))", "','.join(map(str, (*numbers, revision)))")
replace('_internal/tools/nsis_builder.py',
'''    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('A stable numeric application version is required.')''',
'''    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z])?', version):
        raise ValueError('A numeric version or a single-letter revision is required.')
    base, _, suffix = version.partition('-')
    revision = ord(suffix) - ord('a') + 1 if suffix else 0
    if any(int(part) > 65535 for part in base.split('.')):
        raise ValueError('Native version components must fit the Windows version resource.')
    numeric_version = base + '.' + str(revision)''')
replace('_internal/tools/nsis_builder.py',
        "definitions = {'VERSION': version, 'STAGING': nsis_literal(staging),",
        "definitions = {'VERSION': version, 'NUMERIC_VERSION': numeric_version,\n                   'STAGING': nsis_literal(staging),")
replace('_internal/tools/installer_bootstrap.nsi',
        'VIProductVersion "${VERSION}.0"', 'VIProductVersion "${NUMERIC_VERSION}"')
p = Path('README.md')
s = p.read_text(encoding='utf-8')
s = re.sub(r'\A# 文字起こし統合スタジオ v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z])?',
           '# 文字起こし統合スタジオ v1.0.0-a', s, count=1)
p.write_text(s, encoding='utf-8', newline='\n')
p = Path('_internal/docs/CHANGELOG.md')
s = p.read_text(encoding='utf-8')
s = '# v1.0.0-a\n\n発話間の空行を既定0行（設定で0〜10行）に変更し、話者名編集結果を元のTXT／CSVと同じフォルダーへ保存します。\n\nこの版の `-a` は v1.0.0 に対する改修版番号です。Windows内部の数値版は `1.0.0.1`、画面とファイルの表記は `1.0.0-a` です。既存v1.0.0から更新でき、より新しい数値版からのダウングレードは拒否します。\n\n---\n\n' + s
p.write_text(s, encoding='utf-8', newline='\n')
