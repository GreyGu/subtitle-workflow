#!/usr/bin/env python3
"""Read-only SRT audit, review-map checks and explicit non-overwriting normalization.

Python standard library only. No networking, translation or automatic retiming.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import html
import json
import math
from pathlib import Path
import re
import statistics
import sys

TOOL_VERSION = '1.4.0'
MAP_SCHEMA_VERSION = '1.1'
QUALITY_DIMENSIONS = ('faithfulness', 'clarity', 'style')
QUALITY_STATES = ('pass', 'revise', 'uncertain', 'not_reviewed')

STAMP = r'-?\d{1,}:\d{2}:\d{2}[,.]\d{3}'
HEADER = re.compile(
    rf'(?m)^[ \t]*(?P<id>\d+)[ \t]*\n(?:[ \t]*\n)*'
    rf'[ \t]*(?P<start>{STAMP})[ \t]*-->[ \t]*'
    rf'(?P<end>{STAMP})(?P<settings>[^\n]*)(?:\n|$)'
)
MARKUP = re.compile(r'</?(?:b|i|u|s|font|ruby|rt)\b[^>]*>|\{\\[^}]*\}', re.I)
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
PROMO = re.compile(r'getray\.app|tryray\.app|opensubtitles\.(?:org|com)|subscene\.com', re.I)


def issue(items, severity, code, message, cue_id=None):
    row = dict(severity=severity, code=code, message=message)
    if cue_id is not None:
        row['cue_id'] = cue_id
    items.append(row)


def to_ms(value):
    negative = value.startswith('-')
    h, m, s, frac = map(int, re.split(r'[:,.]', value.lstrip('-')))
    if m >= 60 or s >= 60:
        raise ValueError('Minutes and seconds must be below 60: '+value)
    result = ((h * 60 + m) * 60 + s) * 1000 + frac
    return -result if negative else result


def timestamp(value):
    if value < 0:
        raise ValueError('Negative timestamps cannot be normalized.')
    h, rest = divmod(value, 3600000)
    m, rest = divmod(rest, 60000)
    s, frac = divmod(rest, 1000)
    return f'{h:02}:{m:02}:{s:02},{frac:03}'


def visible(text):
    return html.unescape(MARKUP.sub('', text)).strip()


def parse_srt(text):
    """Do not use blank lines as cue delimiters; do not skip broken time lines."""
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    matches = list(HEADER.finditer(text))
    cues, issues = [], []
    if not matches:
        issue(issues, 'error', 'no_cues', 'No numbered SRT timestamp headers were found.')
        return cues, issues
    if text[:matches[0].start()].strip():
        issue(issues, 'error', 'unparsed_prefix', 'Nonblank content precedes the first valid cue.')
    timing_lines = {text.count('\n', 0, match.start('start')) + 1 for match in matches}
    for number, line in enumerate(text.splitlines(), 1):
        if '-->' in line and number not in timing_lines:
            issue(issues, 'error', 'unparsed_timing_line', f'Timing-like line {number} was not parsed; inspect it manually.')
    for i, match in enumerate(matches):
        end_pos = matches[i+1].start() if i+1 < len(matches) else len(text)
        body = '\n'.join(line.rstrip() for line in text[match.end():end_pos].splitlines() if line.strip()).strip()
        cue_id = int(match['id'])
        try:
            start, end = to_ms(match['start']), to_ms(match['end'])
        except ValueError as exc:
            issue(issues, 'error', 'invalid_timestamp', str(exc), cue_id)
            continue
        cues.append(dict(id=cue_id, start_ms=start, end_ms=end, text=body,
                         settings=match['settings'].strip(),
                         source_line=text.count('\n', 0, match.start()) + 1))
    return cues, issues


def load_srt(path, encoding='utf-8-sig'):
    path = Path(path)
    if path.suffix.lower() != '.srt':
        raise ValueError('Only .srt input is supported; use a format-specific tool for ASS/SSA/SUP/IDX/SUB.')
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Unexpectedly large SRT; verify the file type before reading it.')
    raw = path.read_bytes()
    text = raw.decode(encoding, errors='strict')
    cues, issues = parse_srt(text)
    return raw, cues, issues


def audit_cues(cues, initial=None, duration_ms=None, max_cps=22.0,
               max_line_length=42, max_lines=2, gap_ms=45000):
    issues = list(initial or [])
    seen, payloads = set(), set()
    max_end = None
    previous_start = None
    metrics = []
    for ordinal, cue in enumerate(cues, 1):
        cue_id, start, end = cue['id'], cue['start_ms'], cue['end_ms']
        body = visible(cue['text'])
        if cue_id in seen:
            issue(issues, 'error', 'duplicate_id', 'Cue number occurs more than once.', cue_id)
        seen.add(cue_id)
        if cue_id != ordinal:
            issue(issues, 'warning', 'nonsequential_id', f'Physical position is {ordinal}.', cue_id)
        if not body:
            issue(issues, 'error', 'empty_text', 'Cue has no visible subtitle text.', cue_id)
        if '\ufffd' in cue['text']:
            issue(issues, 'error', 'replacement_character', 'Unicode replacement character found; recheck original encoding.', cue_id)
        if CONTROL.search(cue['text']):
            issue(issues, 'error', 'control_character', 'Unexpected control character in subtitle text.', cue_id)
        if '\ufeff' in cue['text']:
            issue(issues, 'warning', 'embedded_bom', 'Encoding signature occurs inside a cue.', cue_id)
        if start < 0 or end < 0:
            issue(issues, 'error', 'negative_time', 'Negative display time.', cue_id)
        if end <= start:
            issue(issues, 'error', 'invalid_interval', 'End time is not later than start time.', cue_id)
        if previous_start is not None and start < previous_start:
            issue(issues, 'error', 'backward_start', 'Start time goes backwards in file order.', cue_id)
        if max_end is not None:
            if start < max_end:
                issue(issues, 'warning', 'overlap', f'Overlaps an earlier cue by at least {max_end-start} ms; may be intentional.', cue_id)
            elif gap_ms is not None and start - max_end > gap_ms:
                issue(issues, 'warning', 'large_gap', f'Gap of {start-max_end} ms is a review lead, not proof of missing dialogue.', cue_id)
        max_end = end if max_end is None else max(max_end, end)
        previous_start = start
        if duration_ms is not None and end > duration_ms:
            issue(issues, 'error', 'beyond_video', f'Cue ends after video duration {duration_ms} ms.', cue_id)
        key = (start, end, body)
        if key in payloads:
            issue(issues, 'warning', 'duplicate_cue', 'Identical text and display interval repeated.', cue_id)
        payloads.add(key)
        lines = body.splitlines()
        line_length = max(map(len, lines), default=0)
        count = len(body.replace('\n', ' '))
        cps = round(count * 1000 / (end-start), 3) if end > start else None
        metrics.append(dict(id=cue_id, cps=cps, longest_line=line_length, lines=len(lines)))
        if cps is not None and cps > max_cps:
            issue(issues, 'warning', 'high_cps', f'{cps} characters/second exceeds review threshold {max_cps}.', cue_id)
        if line_length > max_line_length:
            issue(issues, 'warning', 'long_line', f'{line_length} characters exceeds review threshold {max_line_length}.', cue_id)
        if len(lines) > max_lines:
            issue(issues, 'warning', 'many_lines', f'{len(lines)} display lines exceeds review threshold {max_lines}.', cue_id)
        if PROMO.search(cue['text']):
            issue(issues, 'warning', 'possible_promo_or_credit', 'Verify whether this is an ad, attribution, or real film content; never auto-delete.', cue_id)
    counts = Counter(x['severity'] for x in issues)
    return dict(
        status='errors' if counts['error'] else ('needs_review' if counts['warning'] else 'structure_pass'),
        summary=dict(cues=len(cues), empty_cues=sum(not visible(c['text']) for c in cues),
                     errors=counts['error'], warnings=counts['warning'],
                     first_start_ms=min((c['start_ms'] for c in cues), default=None),
                     last_end_ms=max((c['end_ms'] for c in cues), default=None),
                     max_cps=max((m['cps'] for m in metrics if m['cps'] is not None), default=None),
                     max_line_length=max((m['longest_line'] for m in metrics), default=0)),
        issues=issues, metrics=metrics, cues=cues,
        limitation='Structure and readability only; translation, reference coverage and audio synchronization are not certified.')


def audit_file(path, encoding='utf-8-sig', **options):
    raw, cues, issues = load_srt(path, encoding)
    report = audit_cues(cues, issues, **options)
    report.update(file=str(Path(path).resolve()), bytes=len(raw), encoding=encoding,
                  sha256=hashlib.sha256(raw).hexdigest())
    return report


def write_new(path, raw, protected=()):
    dest = Path(path).resolve()
    if any(dest == Path(p).resolve() for p in protected):
        raise ValueError('Output must not be an input file.')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('xb') as handle:
        handle.write(raw)


def normalize(source, output, encoding='utf-8-sig'):
    if Path(output).suffix.lower() != '.srt':
        raise ValueError('Normalized output must use .srt.')
    report = audit_file(source, encoding)
    if report['summary']['errors']:
        raise ValueError('Normalization refused: resolve structural errors, including empty cues, first.')
    blocks = []
    for cue in report['cues']:
        settings = ' '+cue['settings'] if cue['settings'] else ''
        blocks.append(f"{cue['id']}\n{timestamp(cue['start_ms'])} --> {timestamp(cue['end_ms'])}{settings}\n{cue['text']}")
    rendered = '\n\n'.join(blocks)+'\n'
    check, errors = parse_srt(rendered)
    keys = ('id', 'start_ms', 'end_ms', 'text', 'settings')
    if errors or [tuple(c[k] for k in keys) for c in check] != [tuple(c[k] for k in keys) for c in report['cues']]:
        raise ValueError('Round-trip changed subtitle content; refusing to write.')
    raw = rendered.replace('\n', '\r\n').encode('utf-8-sig')
    write_new(output, raw, protected=(source,))
    if Path(output).read_bytes() != raw:
        raise OSError('Written copy does not match prepared bytes.')
    return dict(output=str(Path(output).resolve()), cues=len(check), bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(), content_and_times_preserved=True,
                warnings_to_review=report['summary']['warnings'])


def validate_review_metadata(mapping, required=False):
    """Validate declared provenance, not the truth of language-review claims."""
    version = mapping.get('schema_version')
    if 'schema_version' in mapping and version != MAP_SCHEMA_VERSION:
        raise ValueError('Unsupported map schema_version; supported version is '+MAP_SCHEMA_VERSION+'.')
    if required and version != MAP_SCHEMA_VERSION:
        raise ValueError('--require-quality-review needs schema_version '+MAP_SCHEMA_VERSION+'.')
    if version is None:
        return
    metadata = mapping.get('metadata')
    if not isinstance(metadata, dict):
        raise ValueError('Versioned maps require metadata.')
    languages = metadata.get('source_languages')
    language_pattern = r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*'
    if (not isinstance(languages, list) or not languages
            or any(not isinstance(x, str) or not re.fullmatch(language_pattern, x) for x in languages)
            or len(set(x.lower() for x in languages)) != len(languages)):
        raise ValueError('source_languages must list distinct language tags, for example cmn, yue, en, or ru.')
    target_language = metadata.get('target_language')
    if not isinstance(target_language, str) or not re.fullmatch(language_pattern, target_language):
        raise ValueError('target_language must be a language tag, for example en or zh-Hans.')
    if 'target_convention' in metadata and not isinstance(metadata['target_convention'], str):
        raise ValueError('target_convention must be text when present.')
    for field in ('reviewer', 'reference_basis', 'reviewed_at'):
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            raise ValueError('metadata needs a nonempty '+field+'.')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', metadata['reviewed_at']):
        raise ValueError('reviewed_at must be a YYYY-MM-DD date.')
    date.fromisoformat(metadata['reviewed_at'])


def check_map(target, reference, mapping, tolerance_ms=250, require_quality_review=False):
    issues, groups = [], []
    if not isinstance(mapping, dict):
        raise ValueError('Mapping root must be a JSON object.')
    validate_review_metadata(mapping, require_quality_review)
    target_by_id = {c['id']: c for c in target}
    ref_by_id = {c['id']: c for c in reference}
    if len(target_by_id) != len(target) or len(ref_by_id) != len(reference):
        raise ValueError('Duplicate cue numbers make the review map ambiguous.')
    used_t, used_r = set(), set()
    checked, unreviewed, uncertain = 0, 0, 0
    quality_counts = {key: Counter() for key in QUALITY_DIMENSIONS}
    quality_records = 0

    def check_quality(record, label):
        nonlocal quality_records
        quality = record.get('quality_review')
        if 'quality_review' in record and quality is None:
            raise ValueError(label+' quality_review must be an object, not null.')
        if quality is None and not require_quality_review:
            return None
        if quality is None:
            quality = {}
        if not isinstance(quality, dict) or set(quality)-set(QUALITY_DIMENSIONS):
            raise ValueError(label+' quality_review must use faithfulness, clarity, and style only.')
        quality_records += 1
        states = {}
        for dimension in QUALITY_DIMENSIONS:
            entry = quality.get(dimension, {'status':'not_reviewed'})
            if not isinstance(entry, dict) or set(entry)-{'status','note'}:
                raise ValueError(label+' '+dimension+' needs status and optional note.')
            status, note = entry.get('status'), entry.get('note', '')
            if status not in QUALITY_STATES or not isinstance(note, str):
                raise ValueError(label+' '+dimension+' has an invalid status or note.')
            if status != 'not_reviewed' and not note.strip():
                raise ValueError(label+' '+dimension+' requires a concrete review note.')
            quality_counts[dimension][status] += 1
            states[dimension] = status
            if status != 'pass':
                issue(issues, 'warning', 'quality_'+status, label+': '+dimension+' is '+status+'.')
        return states

    def claim(ids, known, used, label):
        if not isinstance(ids, list) or not ids or any(type(x) is not int for x in ids):
            raise ValueError(label+' must be a nonempty list of integer cue IDs.')
        if len(set(ids)) != len(ids) or set(ids) & used:
            raise ValueError(label+' was assigned more than once; group splits/merges into a single link.')
        unknown = set(ids) - known.keys()
        if unknown:
            raise ValueError(label+' contains unknown IDs: '+str(sorted(unknown)))
        used.update(ids)

    links = mapping.get('links', [])
    if not isinstance(links, list):
        raise ValueError('links must be a list.')
    for index, link in enumerate(links, 1):
        if not isinstance(link, dict):
            raise ValueError('Every link must be an object.')
        tids, rids = link.get('target_ids'), link.get('reference_ids')
        claim(tids, target_by_id, used_t, 'target_ids')
        claim(rids, ref_by_id, used_r, 'reference_ids')
        review = link.get('review', 'unreviewed')
        if review not in ('checked', 'unreviewed', 'uncertain'):
            raise ValueError('review must be checked, unreviewed, or uncertain.')
        checked += review == 'checked'
        unreviewed += review == 'unreviewed'
        uncertain += review == 'uncertain'
        quality_states = check_quality(link, f'Link {index}')
        tc, rc = [target_by_id[x] for x in tids], [ref_by_id[x] for x in rids]
        rs, re_ = min(c['start_ms'] for c in rc), max(c['end_ms'] for c in rc)
        start_error = min(c['start_ms'] for c in tc)-rs
        end_error = max(c['end_ms'] for c in tc)-re_
        groups.append(dict(link=index, target_ids=tids, reference_ids=rids,
                           reference_start_ms=rs, reference_end_ms=re_,
                           start_error_ms=start_error, end_error_ms=end_error, review=review,
                           group_boundaries_only=len(tids)>1 or len(rids)>1,
                           quality_review_declared=quality_states))
        if max(abs(start_error), abs(end_error)) > tolerance_ms:
            issue(issues, 'warning', 'timing_difference', f'Link {index}: start {start_error} ms, end {end_error} ms.')
    excluded = mapping.get('excluded_reference', [])
    target_only = mapping.get('target_only', [])
    exclusions = Counter()
    for rows, known, used, label, allowed in (
        (excluded, ref_by_id, used_r, 'excluded_reference', {'non_speech','credit','untranscribed_speech','source_ad','not_in_target_cut','unclassified'}),
        (target_only, target_by_id, used_t, 'target_only', {'opening_credit','screen_text','verified_audio_speech','source_credit'}),
    ):
        if not isinstance(rows, list):
            raise ValueError(label+' must be a list.')
        for row in rows:
            if (not isinstance(row, dict) or row.get('category') not in allowed
                    or not isinstance(row.get('reason'), str) or not row['reason'].strip()):
                raise ValueError(label+' needs an allowed category and a nonempty reason.')
            claim([row.get('id')], known, used, label)
            if label == 'excluded_reference':
                exclusions[row['category']] += 1
            else:
                check_quality(row, 'Target-only cue '+str(row['id']))
    missing_t, missing_r = sorted(target_by_id.keys()-used_t), sorted(ref_by_id.keys()-used_r)
    if missing_r:
        issue(issues, 'warning', 'unaccounted_reference', f'{len(missing_r)} reference cues not accounted for.')
    if missing_t:
        issue(issues, 'warning', 'unaccounted_target', f'{len(missing_t)} target cues not accounted for.')
    if unreviewed or uncertain:
        issue(issues, 'warning', 'unfinished_language_review', f'{unreviewed} unreviewed and {uncertain} uncertain links.')
    if exclusions['untranscribed_speech']:
        issue(issues, 'warning', 'untranscribed_speech', 'Untranscribed speech remains; full audio coverage is not established.')
    if exclusions['not_in_target_cut']:
        issue(issues, 'warning', 'different_cut', 'Verify every excluded scene against the actual target edit.')
    if exclusions['unclassified']:
        issue(issues, 'warning', 'unclassified_reference', 'Some reference records have no reviewed classification yet.')
    groups.sort(key=lambda x:x['reference_start_ms'])
    deltas = [g['start_error_ms'] for g in groups]
    return dict(
        status='needs_review' if issues else 'map_records_pass',
        tool_version=TOOL_VERSION, map_schema_version=mapping.get('schema_version', 'legacy'),
        quality_review_required=require_quality_review,
        summary=dict(target_cues=len(target), reference_cues=len(reference), links=len(links),
                     language_checked_links_declared=checked, unreviewed_links=unreviewed, uncertain_links=uncertain,
                     accounted_reference_cues=len(used_r),
                     reference_accounted_percent=round(len(used_r)*100/len(reference), 3) if reference else None,
                     reference_exclusions=dict(exclusions), tolerance_ms=tolerance_ms,
                     quality_review_evaluated_records=quality_records,
                     quality_review_declared={key:{state:quality_counts[key][state] for state in QUALITY_STATES}
                                              for key in QUALITY_DIMENSIONS},
                     max_abs_start_error_ms=max(map(abs,deltas),default=None),
                     median_start_error_ms=statistics.median(deltas) if deltas else None,
                     max_abs_end_error_ms=max((abs(g['end_error_ms']) for g in groups),default=None)),
        unaccounted_reference_ids=missing_r, unaccounted_target_ids=missing_t,
        issues=issues, groups=groups,
        limitation='Checks declared mapping/quality records and group timing only. A pass verifies records, not truth of faithfulness, clarity, style, or full-audio coverage.')


def validate_map_fingerprints(mapping, target_report, reference_report):
    for key, report in [('target_sha256', target_report), ('reference_sha256', reference_report)]:
        recorded = mapping.get(key)
        if not isinstance(recorded, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', recorded):
            raise ValueError('Mapping requires '+key+' from the exact reviewed input file.')
        if recorded.lower() != report['sha256'].lower():
            raise ValueError(key+' changed; recheck cue IDs and language reviews before reusing this map.')


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--version', action='version', version='%(prog)s '+TOOL_VERSION)
    commands = cli.add_subparsers(dest='command', required=True)
    audit = commands.add_parser('audit', help='Read-only full-file structural audit.')
    audit.add_argument('input')
    audit.add_argument('--encoding', default='utf-8-sig')
    audit.add_argument('--duration-seconds', type=float)
    audit.add_argument('--max-cps', type=float, default=22.0)
    audit.add_argument('--max-line-length', type=int, default=42)
    audit.add_argument('--max-lines', type=int, default=2)
    audit.add_argument('--gap-seconds', type=float, default=45)
    audit.add_argument('--json', dest='json_output')
    norm = commands.add_parser('normalize', help='Write a new UTF-8-BOM/CRLF file without retiming or deleting content.')
    norm.add_argument('input')
    norm.add_argument('--encoding', default='utf-8-sig')
    norm.add_argument('--output', required=True)
    mapping = commands.add_parser('map-check', help='Audit an explicitly reviewed many-to-many cue map.')
    mapping.add_argument('--target', required=True)
    mapping.add_argument('--reference', required=True)
    mapping.add_argument('--mapping', required=True)
    mapping.add_argument('--target-encoding', default='utf-8-sig')
    mapping.add_argument('--reference-encoding', default='utf-8-sig')
    mapping.add_argument('--tolerance-ms', type=int, default=250)
    mapping.add_argument('--require-quality-review', action='store_true',
                         help='Require a versioned map and complete declared faithfulness, clarity, and style reviews.')
    mapping.add_argument('--json', dest='json_output')
    args = cli.parse_args(argv)
    try:
        if args.command == 'normalize':
            result = normalize(args.input, args.output, args.encoding)
            code, protected = 0, [args.input]
        elif args.command == 'audit':
            thresholds = (args.max_cps,args.max_line_length,args.max_lines,args.gap_seconds)
            if any(not math.isfinite(x) or x <= 0 for x in thresholds):
                raise ValueError('Readability and gap thresholds must be finite and positive.')
            if args.duration_seconds is not None and (not math.isfinite(args.duration_seconds) or args.duration_seconds <= 0):
                raise ValueError('Video duration must be finite and positive.')
            result = audit_file(args.input,args.encoding,
                                duration_ms=round(args.duration_seconds*1000) if args.duration_seconds is not None else None,
                                max_cps=args.max_cps,max_line_length=args.max_line_length,
                                max_lines=args.max_lines,gap_ms=round(args.gap_seconds*1000))
            code, protected = (2 if result['summary']['errors'] else 0), [args.input]
        else:
            if args.tolerance_ms < 0:
                raise ValueError('Tolerance must not be negative.')
            target = audit_file(args.target,args.target_encoding)
            reference = audit_file(args.reference,args.reference_encoding)
            if target['summary']['errors'] or reference['summary']['errors']:
                raise ValueError('Resolve structural errors in target/reference before map-check.')
            data = json.loads(Path(args.mapping).read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                raise ValueError('Mapping root must be an object.')
            validate_map_fingerprints(data,target,reference)
            result = check_map(target['cues'],reference['cues'],data,args.tolerance_ms,args.require_quality_review)
            result['inputs'] = {name:{'file':report['file'],'sha256':report['sha256']} for name,report in [('target',target),('reference',reference)]}
            code = 3 if result['status']=='needs_review' else 0
            protected = [args.target,args.reference,args.mapping]
        result['tool_version'] = TOOL_VERSION
        output = getattr(args,'json_output',None)
        if output:
            write_new(output,(json.dumps(result,ensure_ascii=False,indent=2)+'\n').encode('utf-8'),protected)
            print(json.dumps(dict(report=str(Path(output).resolve()),status=result['status'],summary=result['summary']),ensure_ascii=False,indent=2))
        else:
            print(json.dumps(result,ensure_ascii=False,indent=2))
        return code
    except (OSError, ValueError, LookupError) as exc:
        print(json.dumps({'status':'error','message':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
