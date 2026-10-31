"""Integrity gate for trusted private-main GitHub Actions runtime caches.

The GitHub cache service's repository/branch scope authenticates the producer;
this manifest detects corruption and input mismatches, not a malicious cache
producer. Public and tag builds never restore these caches. No cached Python
or DLL is executed until the entire file set has been checked by host Python.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat

import build_windows as build

REPO = Path(__file__).resolve().parents[2]
MANIFEST = 'runtime-cache-manifest.json'
INPUTS = ('.github/workflows/windows-release.yml', '_internal/tools/build_windows.py',
    '_internal/tools/build_cpython.py', '_internal/tools/runtime_cache.py',
    '_internal/tools/cpython-windows-sources.json', '_internal/tools/windows-build-sources.json',
    '_internal/tools/requirements-build.lock', '_internal/tools/requirements-tests-windows.lock')


def header() -> dict:
    image = os.environ.get('ImageVersion', '')
    repository_id = os.environ.get('GITHUB_REPOSITORY_ID', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', image) or not re.fullmatch(r'[1-9][0-9]{0,18}', repository_id):
        raise ValueError('An exact hosted image version and numeric repository ID are required.')
    files = {name:build.digest(REPO/name) for name in INPUTS}
    return {'schema':1, 'repository_id':repository_id, 'image_version':image,
            'host_python':platform.python_version(), 'architecture':'x64', 'inputs':files}


def key(value: dict) -> str:
    digest = hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return 'unified-runtime-v1-' + value['repository_id'] + '-' + digest


def permitted() -> bool:
    return (os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
            and os.environ.get('GITHUB_REF') == 'refs/heads/main'
            and os.environ.get('PRIVATE_REPOSITORY') == 'true')


def ordinary(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
        raise ValueError('Runtime caches cannot contain symlinks or reparse points.')
    return info


def inventory(work: Path) -> dict:
    files = {}
    folded = set()
    total = 0
    def add(path: Path):
        nonlocal total
        info = ordinary(path)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Only ordinary runtime files can be cached.')
        name = path.relative_to(work).as_posix()
        if name.casefold() in folded:
            raise ValueError('A Windows case-conflicting runtime path was found.')
        folded.add(name.casefold())
        total += info.st_size
        if info.st_size > 128*1024*1024 or total > 1024*1024*1024 or len(files) >= 20000:
            raise ValueError('Runtime cache exceeds its file/size bounds.')
        files[name] = {'size':info.st_size,'sha256':build.digest(path)}
    def walk(path: Path):
        if not stat.S_ISDIR(ordinary(path).st_mode):
            raise ValueError('Runtime must be an ordinary directory.')
        for child in sorted(path.iterdir()):
            info = ordinary(child)
            if stat.S_ISDIR(info.st_mode):
                walk(child)
            else:
                add(child)
    walk(work/'Runtime')
    add(work/'runtime-info.json')
    return files


def read_json(path: Path, maximum: int) -> dict:
    if not stat.S_ISREG(ordinary(path).st_mode) or path.stat().st_size > maximum:
        raise ValueError('Runtime cache metadata is not an ordinary bounded JSON file.')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):
        raise ValueError('Runtime cache metadata must be an object.')
    return value


def validate_identity(work: Path) -> dict:
    identity = read_json(work/'runtime-info.json',2*1024*1024)
    build.validate_runtime_versions(identity)
    locked = json.loads((REPO/'_internal/tools/cpython-windows-sources.json').read_text(encoding='utf-8'))
    source = identity.get('source_build',{})
    if source.get('source') != locked or source.get('security_regression') != 'test.test_ssl.SSLObjectTests.test_distrusted_spki passed':
        raise ValueError('The cached source inputs or mandatory TLS regression record do not match.')
    return identity


def seal(work: Path) -> None:
    if not permitted():
        raise ValueError('Runtime cache saving is restricted to private manual main builds.')
    validate_identity(work)
    build.write_json(work/MANIFEST, {'header':header(), 'producer':{
        'ref':'refs/heads/main', 'commit':os.environ.get('GITHUB_SHA',''),
        'run_id':os.environ.get('GITHUB_RUN_ID','')}, 'files':inventory(work)})


def verify_bytes(work: Path, expected_header: dict) -> dict:
    manifest = read_json(work/MANIFEST,8*1024*1024)
    if set(manifest) != {'header','producer','files'} or manifest['header'] != expected_header:
        raise ValueError('Runtime cache input header does not match this exact build.')
    producer = manifest['producer']
    if (not isinstance(producer,dict) or producer.get('ref') != 'refs/heads/main'
            or not re.fullmatch('[0-9a-f]{40}',producer.get('commit',''))
            or not re.fullmatch('[1-9][0-9]*',producer.get('run_id',''))):
        raise ValueError('Runtime cache lacks a valid private-main producer record.')
    # Compare the complete actual set rather than iterating cached filenames:
    # cached metadata cannot choose paths to read or omit an injected file.
    if inventory(work) != manifest['files']:
        raise ValueError('Runtime cache has changed, missing or additional files.')
    return validate_identity(work)


def verify(work: Path) -> None:
    if not permitted():
        raise ValueError('Runtime cache restoration is restricted to private manual main builds.')
    identity = verify_bytes(work,header())
    build.require_windows()
    # Recheck today's host SDK signatures/revocation policy and require the
    # cached CRT bytes to be identical BEFORE starting the cached interpreter.
    preflight = work/'cache-crt-preflight'
    preflight.mkdir()
    crt = build.bundle_visual_cpp_runtime(preflight)
    for file in crt['files']:
        if build.digest(work/'Runtime'/file['name']) != file['sha256']:
            raise ValueError('Cached CRT differs from the currently verified Microsoft SDK.')
    build.probe_runtime(work, crt=crt, source_build=identity['source_build'])
    print('Exact private-main cache passed complete byte, input, CRT signature and native runtime checks.')


def discard(work: Path) -> None:
    """Remove only fixed cache members after an inexact cache-key result."""
    for name in ('Runtime','runtime-info.json',MANIFEST):
        path = work/name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode):
            if getattr(info,'st_file_attributes',0) & 0x400:
                path.rmdir()
            else:
                shutil.rmtree(path)
        else:
            path.unlink()


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('key','seal','verify','discard'))
    parser.add_argument('--work',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='key':
        output=os.environ['GITHUB_OUTPUT']
        with open(output,'a',encoding='utf-8') as stream:
            stream.write('key='+key(header())+'\n')
    elif args.command=='seal':seal(args.work)
    elif args.command=='verify':verify(args.work)
    else:discard(args.work)


if __name__=='__main__':
    main()
