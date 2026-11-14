#!/usr/bin/env python3
"""Build or verify a small allowlisted skill ZIP. No network, extraction, or execution."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import zipfile

from subtitle_audit import TOOL_VERSION

SKILL_NAME = 'subtitle-workflow'
RELEASE_FILES = (
    'SKILL.md', 'README.md', 'LICENSE', 'agents/openai.yaml',
    'references/case-study-and-paths.md', 'references/full-review.md',
    'references/retrieval-and-files.md', 'references/script-usage.md',
    'references/translation-quality.md', 'references/language-directions.md',
    'references/token-efficient-workflow.md',
    'assets/review-map.schema.json', 'assets/review-report-template.md', 'assets/task-brief-template.md',
    'assets/examples/zh-en/source.zh.srt', 'assets/examples/zh-en/target.en.srt',
    'assets/examples/zh-en/review-map.json',
    'scripts/subtitle_audit.py', 'scripts/subtitle_progress.py', 'scripts/test_subtitle_audit.py',
    'scripts/package_skill.py', 'scripts/test_package_skill.py',
)
MANIFEST_NAME = SKILL_NAME+'/MANIFEST.json'
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def build(output, root=None):
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    output = Path(output)
    if output.suffix.lower() != '.zip':
        raise ValueError('Output must use .zip.')
    payloads, files = {}, []
    for name in sorted(RELEASE_FILES):
        path = root/name
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(root) or not path.is_file():
            raise ValueError('Missing or unsafe release file: '+name)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('Unexpectedly large release file: '+name)
        raw = path.read_bytes()
        payloads[SKILL_NAME+'/'+name] = raw
        files.append(dict(path=name, bytes=len(raw), sha256=digest(raw)))
    manifest = dict(manifest_version=1, skill_name=SKILL_NAME, skill_version=TOOL_VERSION, files=files)
    payloads[MANIFEST_NAME] = (json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    if sum(map(len,payloads.values())) > MAX_ARCHIVE_BYTES:
        raise ValueError('Release payload is unexpectedly large.')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(payloads.items()):
            info = zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info,raw)
    prepared = buffer.getvalue()
    if len(prepared) > MAX_ARCHIVE_BYTES:
        raise ValueError('ZIP is unexpectedly large.')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as handle:
        handle.write(prepared)
    if output.read_bytes() != prepared:
        raise OSError('Written archive differs from prepared bytes.')
    return verify(output)


def verify(path):
    path = Path(path)
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('Archive exceeds the review size limit.')
    raw_archive = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw_archive),'r') as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        expected = {SKILL_NAME+'/'+name for name in RELEASE_FILES} | {MANIFEST_NAME}
        if len(names) != len(set(names)) or set(names) != expected:
            raise ValueError('Archive contains duplicate, missing, unexpected, or unsafe paths.')
        if any(info.file_size > MAX_FILE_BYTES or info.flag_bits & 1 for info in infos):
            raise ValueError('Oversized or encrypted member is not allowed.')
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
            raise ValueError('Expanded archive exceeds the review size limit.')
        manifest = json.loads(archive.read(MANIFEST_NAME).decode('utf-8'))
        if (not isinstance(manifest,dict) or manifest.get('manifest_version') != 1
                or manifest.get('skill_name') != SKILL_NAME or manifest.get('skill_version') != TOOL_VERSION):
            raise ValueError('Unsupported release manifest.')
        files = manifest.get('files')
        if not isinstance(files,list):
            raise ValueError('Manifest files must be a list.')
        seen = set()
        for row in files:
            if not isinstance(row,dict):
                raise ValueError('Every manifest file must be an object.')
            name, size, sha = row.get('path'), row.get('bytes'), row.get('sha256')
            if not isinstance(name,str) or name not in RELEASE_FILES or name in seen:
                raise ValueError('Manifest contains an invalid or duplicate path.')
            if type(size) is not int or size < 0 or not isinstance(sha,str) or not re.fullmatch(r'[0-9a-f]{64}',sha):
                raise ValueError('Manifest size or SHA-256 is invalid.')
            seen.add(name)
            raw = archive.read(SKILL_NAME+'/'+name)
            if len(raw) != size or digest(raw) != sha:
                raise ValueError('File fingerprint mismatch: '+name)
        if seen != set(RELEASE_FILES):
            raise ValueError('Manifest does not cover exactly the release files.')
    return dict(status='package_verified',skill_name=SKILL_NAME,skill_version=TOOL_VERSION,
                file=str(path.resolve()),bytes=len(raw_archive),sha256=digest(raw_archive),files=len(seen),
                limitation='Integrity against the included manifest only; not a signature, trust assertion, or code-quality certificate.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    make = commands.add_parser('build',help='Build a new ZIP using the explicit file allowlist.')
    make.add_argument('--output',required=True)
    check = commands.add_parser('verify',help='Verify file membership and hashes without extracting or executing.')
    check.add_argument('archive')
    args = parser.parse_args(argv)
    try:
        report = build(args.output) if args.command=='build' else verify(args.archive)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0
    except (OSError,ValueError,zipfile.BadZipFile,RuntimeError) as exc:
        print(json.dumps(dict(status='error',message=str(exc)),ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
