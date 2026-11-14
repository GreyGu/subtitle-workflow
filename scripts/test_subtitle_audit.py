import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import subtitle_audit as audit


def cue(number, start, end, text):
    return {'id':number,'start_ms':start,'end_ms':end,'text':text,'settings':'','source_line':1}


class ParsingTests(unittest.TestCase):
    def codes(self, text):
        cues, issues = audit.parse_srt(text)
        return {i['code'] for i in audit.audit_cues(cues,issues)['issues']}

    def test_blank_lines_inside_headers_and_body_do_not_lose_text(self):
        text = '1\n\n00:00:01,000 --> 00:00:03,000\n\nПривет.\n\nВторая строка.\n\n2\n00:00:04,000 --> 00:00:05,000\nПока.\n'
        cues, errors = audit.parse_srt(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]['text'], 'Привет.\nВторая строка.')

    def test_double_cr_line_endings(self):
        text = '1\r\r\n00:00:01,000 --> 00:00:02,000\r\r\n你好\r\r\n'
        self.assertEqual(audit.parse_srt(text)[0][0]['text'], '你好')

    def test_empty_cue_and_markup_only_cue_fail(self):
        for body in ('', '<i></i>', '{\\an8}'):
            self.assertIn('empty_text', self.codes('1\n00:00:01,000 --> 00:00:02,000\n'+body))

    def test_malformed_timestamp_not_swallowed_as_dialogue(self):
        text = '1\n00:00:01,000 --> 00:00:02,000\nHi\n\n2\n00:00:bad --> 00:00:04,000\nLost?'
        self.assertIn('unparsed_timing_line', self.codes(text))

    def test_invalid_timestamp_range(self):
        self.assertIn('invalid_timestamp', self.codes('1\n00:65:01,000 --> 00:65:02,000\nHi'))

    def test_negative_and_zero_intervals(self):
        self.assertIn('negative_time', self.codes('1\n-00:00:01,000 --> 00:00:02,000\nHi'))
        self.assertIn('invalid_interval', self.codes('1\n00:00:02,000 --> 00:00:02,000\nHi'))

    def test_replacement_character_is_error(self):
        self.assertIn('replacement_character', self.codes('1\n00:00:01,000 --> 00:00:02,000\n\ufffd'))

    def test_duplicate_ids_are_errors(self):
        result = audit.audit_cues([cue(1,1000,2000,'a'),cue(1,3000,4000,'b')])
        self.assertIn('duplicate_id', [i['code'] for i in result['issues']])

    def test_non_adjacent_overlap_is_detected(self):
        result = audit.audit_cues([cue(1,1000,10000,'a'),cue(2,2000,3000,'b'),cue(3,4000,5000,'c')])
        self.assertEqual([i['cue_id'] for i in result['issues'] if i['code']=='overlap'], [2,3])

    def test_numeric_dialogue_is_preserved(self):
        cues, errors = audit.parse_srt('1\n00:00:01.000 --> 00:00:02.000\n1991\n')
        self.assertEqual(errors, [])
        self.assertEqual(cues[0]['text'], '1991')

    def test_gap_is_not_proof_of_missing_dialogue(self):
        result = audit.audit_cues([cue(1,1000,2000,'a'),cue(2,90000,91000,'b')])
        self.assertEqual(result['summary']['errors'], 0)
        self.assertIn('large_gap', [i['code'] for i in result['issues']])

    def test_video_duration_is_checked(self):
        result = audit.audit_cues([cue(1,1000,3000,'a')],duration_ms=2000)
        self.assertIn('beyond_video', [i['code'] for i in result['issues']])


class FileTests(unittest.TestCase):
    def test_cp1251_strict_decoding_and_normalization(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-audit-test-') as tmp:
            src, dst = Path(tmp)/'original.srt', Path(tmp)/'normalized.srt'
            raw = '1\n00:00:01,000 --> 00:00:03,000\n\nПривет!\n'.encode('cp1251')
            src.write_bytes(raw)
            with self.assertRaises(UnicodeDecodeError):
                audit.load_srt(src)
            result = audit.normalize(src,dst,'cp1251')
            self.assertTrue(result['content_and_times_preserved'])
            self.assertEqual(src.read_bytes(),raw)
            self.assertTrue(dst.read_bytes().startswith(b'\xef\xbb\xbf'))
            self.assertIn(b'\r\n',dst.read_bytes())
            self.assertEqual(audit.load_srt(dst)[1][0]['text'],'Привет!')

    def test_refuses_source_or_existing_output_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-audit-test-') as tmp:
            src,dst=Path(tmp)/'a.srt',Path(tmp)/'b.srt'
            src.write_text('1\n00:00:01,000 --> 00:00:03,000\nHello\n',encoding='utf-8')
            dst.write_text('keep me',encoding='utf-8')
            with self.assertRaises(ValueError): audit.normalize(src,src)
            with self.assertRaises(FileExistsError): audit.normalize(src,dst)
            self.assertEqual(dst.read_text(encoding='utf-8'),'keep me')

    def test_empty_input_cue_cannot_be_normalized(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-audit-test-') as tmp:
            src,dst=Path(tmp)/'a.srt',Path(tmp)/'b.srt'
            src.write_text('1\n00:00:01,000 --> 00:00:03,000\n',encoding='utf-8')
            with self.assertRaises(ValueError): audit.normalize(src,dst)
            self.assertFalse(dst.exists())

    def test_preserves_timestamp_position_settings(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-audit-test-') as tmp:
            src,dst=Path(tmp)/'a.srt',Path(tmp)/'b.srt'
            src.write_text('7\n00:00:01,000 --> 00:00:03,000 X1:40 X2:600\n<i>Hello</i>\n',encoding='utf-8')
            audit.normalize(src,dst)
            record=audit.load_srt(dst)[1][0]
            self.assertEqual(record['id'],7)
            self.assertEqual(record['settings'],'X1:40 X2:600')
            self.assertEqual(record['text'],'<i>Hello</i>')


class MappingTests(unittest.TestCase):
    def test_many_to_one_and_sound_exclusion(self):
        refs=[cue(1,1000,2000,'a'),cue(2,2000,3000,'b'),cue(3,4000,5000,'[wind]')]
        targets=[cue(1,1000,3000,'ab')]
        mapping={'links':[{'reference_ids':[1,2],'target_ids':[1],'review':'checked'}],
                 'excluded_reference':[{'id':3,'category':'non_speech','reason':'Wind only'}]}
        result=audit.check_map(targets,refs,mapping)
        self.assertEqual(result['status'],'map_records_pass')
        self.assertEqual(result['summary']['reference_accounted_percent'],100)
        self.assertTrue(result['groups'][0]['group_boundaries_only'])

    def test_same_count_does_not_hide_missing_mapping(self):
        refs=[cue(1,1000,2000,'a'),cue(2,3000,4000,'b')]
        result=audit.check_map(refs,refs,{'links':[{'reference_ids':[1],'target_ids':[1],'review':'checked'}]})
        self.assertEqual(result['unaccounted_reference_ids'],[2])
        self.assertEqual(result['status'],'needs_review')

    def test_untranscribed_speech_remains_unresolved_even_if_accounted(self):
        result=audit.check_map([], [cue(1,1000,2000,'[Italian]')],
                               {'excluded_reference':[{'id':1,'category':'untranscribed_speech','reason':'No words transcribed'}]})
        self.assertEqual(result['summary']['reference_accounted_percent'],100)
        self.assertEqual(result['status'],'needs_review')

    def test_unreviewed_translation_is_flagged(self):
        c=[cue(1,1000,2000,'a')]
        result=audit.check_map(c,c,{'links':[{'reference_ids':[1],'target_ids':[1]}]})
        self.assertEqual(result['summary']['unreviewed_links'],1)

    def test_duplicate_reference_assignment_is_rejected(self):
        c=[cue(1,1000,2000,'a')]
        row={'reference_ids':[1],'target_ids':[1],'review':'checked'}
        with self.assertRaises(ValueError): audit.check_map(c,c,{'links':[row,row]})

    def test_timing_error_uses_start_and_end(self):
        result=audit.check_map([cue(1,1000,2400,'a')],[cue(1,1000,2000,'a')],
                               {'links':[{'reference_ids':[1],'target_ids':[1],'review':'checked'}]},250)
        self.assertEqual(result['summary']['max_abs_end_error_ms'],400)
        self.assertEqual(result['status'],'needs_review')

    def test_unknown_reference_id_is_rejected(self):
        c=[cue(1,1000,2000,'a')]
        with self.assertRaises(ValueError):
            audit.check_map(c,c,{'links':[{'reference_ids':[99],'target_ids':[1]}]})

    def test_empty_reason_is_not_an_explanation(self):
        for reason in (None, '', '   ', 17):
            with self.assertRaises(ValueError):
                audit.check_map([], [cue(1,1000,2000,'[wind]')],
                                {'excluded_reference':[{'id':1,'category':'non_speech','reason':reason}]})

    def test_changed_file_invalidates_old_map(self):
        digest='a'*64
        with self.assertRaises(ValueError):
            audit.validate_map_fingerprints({'target_sha256':digest,'reference_sha256':'b'*64},
                                            {'sha256':'c'*64},{'sha256':'b'*64})

    def test_map_requires_both_input_fingerprints(self):
        with self.assertRaises(ValueError):
            audit.validate_map_fingerprints({}, {'sha256':'a'*64}, {'sha256':'b'*64})

    def test_map_accepts_equivalent_uppercase_fingerprint(self):
        audit.validate_map_fingerprints({'target_sha256':'A'*64,'reference_sha256':'B'*64},
                                        {'sha256':'a'*64},{'sha256':'b'*64})

    def test_unclassified_reference_remains_pending(self):
        result=audit.check_map([], [cue(1,1000,2000,'[Calling]')],
                               {'excluded_reference':[{'id':1,'category':'unclassified','reason':'Cannot yet tell speech from wordless calling'}]})
        self.assertEqual(result['status'],'needs_review')
        self.assertIn('unclassified_reference',[x['code'] for x in result['issues']])


def quality_map():
    return {
        'schema_version':'1.1',
        'metadata':{'source_languages':['cmn'],'target_language':'en',
                    'reviewer':'Synthetic test; no audio review','reviewed_at':'2026-10-04',
                    'reference_basis':'Original invented text for unit tests'},
        'links':[{'reference_ids':[1],'target_ids':[1],'review':'checked',
                  'quality_review':{key:{'status':'pass','note':'Synthetic test declaration: '+key}
                                    for key in audit.QUALITY_DIMENSIONS}}],
    }


class QualityTests(unittest.TestCase):
    def check(self, mapping):
        return audit.check_map([cue(1,1000,2000,'After you.')],
                               [cue(1,1000,2000,'请您先走。')],mapping,require_quality_review=True)

    def test_three_declared_dimensions_pass_without_claiming_semantic_certification(self):
        result=self.check(quality_map())
        self.assertEqual(result['status'],'map_records_pass')
        self.assertEqual(result['summary']['quality_review_declared']['faithfulness']['pass'],1)
        self.assertIn('not truth',result['limitation'])

    def test_each_unfinished_state_requires_review_despite_checked_mapping(self):
        for state in ('revise','uncertain','not_reviewed'):
            mapping=quality_map()
            mapping['links'][0]['quality_review']['faithfulness']['status']=state
            result=self.check(mapping)
            self.assertEqual(result['status'],'needs_review')
            self.assertEqual(result['summary']['quality_review_declared']['faithfulness'][state],1)

    def test_missing_dimension_is_not_implicitly_passed(self):
        mapping=quality_map()
        del mapping['links'][0]['quality_review']['style']
        result=self.check(mapping)
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(result['summary']['quality_review_declared']['style']['not_reviewed'],1)

    def test_missing_whole_quality_record_remains_pending(self):
        mapping=quality_map()
        del mapping['links'][0]['quality_review']
        result=self.check(mapping)
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(len([x for x in result['issues'] if x['code']=='quality_not_reviewed']),3)

    def test_pass_and_uncertainty_need_nonblank_notes(self):
        for state in ('pass','revise','uncertain'):
            mapping=quality_map()
            mapping['links'][0]['quality_review']['clarity']={'status':state,'note':'  '}
            with self.assertRaises(ValueError): self.check(mapping)

    def test_misspelled_dimensions_states_and_invalid_types_are_rejected(self):
        bad_values=[None,[],{'fidelity':{'status':'pass','note':'x'}},
                    {'style':{'status':'ok','note':'x'}},{'style':{'status':'pass','note':17}}]
        for value in bad_values:
            mapping=quality_map()
            mapping['links'][0]['quality_review']=value
            with self.assertRaises(ValueError): self.check(mapping)

    def test_target_only_text_cannot_bypass_quality_review(self):
        mapping=quality_map()
        mapping['links']=[]
        mapping['excluded_reference']=[{'id':1,'category':'non_speech','reason':'Synthetic sound only'}]
        mapping['target_only']=[{'id':1,'category':'screen_text','reason':'Synthetic title'}]
        result=self.check(mapping)
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(result['summary']['quality_review_evaluated_records'],1)
        mapping['target_only'][0]['quality_review']=quality_map()['links'][0]['quality_review']
        self.assertEqual(self.check(mapping)['status'],'map_records_pass')

    def test_legacy_map_requires_real_upgrade_for_strict_mode(self):
        mapping={'links':[{'reference_ids':[1],'target_ids':[1],'review':'checked'}]}
        with self.assertRaises(ValueError): self.check(mapping)
        c=[cue(1,1000,2000,'x')]
        result=audit.check_map(c,c,mapping)
        self.assertEqual(result['map_schema_version'],'legacy')
        self.assertEqual(result['summary']['quality_review_evaluated_records'],0)

    def test_unsupported_schema_and_incomplete_metadata_fail(self):
        for field,value in [('schema_version','9.0'),('schema_version',None),('metadata',{}),('metadata',None)]:
            mapping=quality_map()
            mapping[field]=value
            with self.assertRaises(ValueError): self.check(mapping)

    def test_language_metadata_and_date_validation(self):
        bad=[('source_languages',['en','EN']),('source_languages',[]),
             ('target_language','English text'),('reviewed_at','2026-02-30'),
             ('reviewed_at','20261004'),('reviewer',' '),('target_convention',None)]
        for field,value in bad:
            mapping=quality_map()
            mapping['metadata'][field]=value
            with self.assertRaises(ValueError): self.check(mapping)

    def test_reverse_and_multilingual_directions_do_not_require_english_reference(self):
        for source,target in [(['en'],'zh-Hans'),(['yue','cmn'],'en-GB'),(['ja'],'ru'),(['ar'],'th')]:
            mapping=quality_map()
            mapping['metadata'].update(source_languages=source,target_language=target)
            self.assertEqual(self.check(mapping)['status'],'map_records_pass')

    def test_provided_quality_is_checked_even_without_strict_option(self):
        mapping=quality_map()
        mapping['links'][0]['quality_review']['style']['status']='revise'
        c=[cue(1,1000,2000,'x')]
        self.assertEqual(audit.check_map(c,c,mapping)['status'],'needs_review')


class PortableAndCliTests(unittest.TestCase):
    def test_multilingual_text_survives_normalization(self):
        bodies=['请您先走。','After you.','Проходите.','مرحبا بكم','สวัสดี','Cafe\u0301']
        with tempfile.TemporaryDirectory(prefix='subtitle-language-test-') as tmp:
            for index,body in enumerate(bodies):
                src,dst=Path(tmp)/f'{index}.srt',Path(tmp)/f'{index}-normalized.srt'
                src.write_text('1\n00:00:01,000 --> 00:00:04,000\n'+body+'\n',encoding='utf-8')
                audit.normalize(src,dst)
                self.assertEqual(audit.load_srt(dst)[1][0]['text'],body)

    def test_original_chinese_to_english_fixture_runs_from_another_working_directory(self):
        root=Path(__file__).resolve().parents[1]
        example=root/'assets/examples/zh-en'
        with tempfile.TemporaryDirectory(prefix='subtitle-cli-test-') as tmp:
            command=[sys.executable,'-B','-X','utf8',str(root/'scripts/subtitle_audit.py'),
                     'map-check','--target',str(example/'target.en.srt'),
                     '--reference',str(example/'source.zh.srt'),'--mapping',str(example/'review-map.json'),
                     '--require-quality-review']
            result=subprocess.run(command,cwd=tmp,capture_output=True,text=True,encoding='utf-8',check=False)
            self.assertEqual(result.returncode,0,result.stderr)
            data=json.loads(result.stdout)
            self.assertEqual(data['summary']['quality_review_evaluated_records'],3)
            self.assertEqual(data['summary']['max_abs_start_error_ms'],0)
            self.assertEqual(data['summary']['reference_accounted_percent'],100)

    def test_cli_incomplete_quality_returns_three(self):
        root=Path(__file__).resolve().parents[1]/'assets/examples/zh-en'
        data=json.loads((root/'review-map.json').read_text(encoding='utf-8'))
        del data['links'][0]['quality_review']['style']
        with tempfile.TemporaryDirectory(prefix='subtitle-cli-test-') as tmp:
            mapping=Path(tmp)/'pending.json'
            mapping.write_text(json.dumps(data),encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code=audit.main(['map-check','--target',str(root/'target.en.srt'),
                                 '--reference',str(root/'source.zh.srt'),'--mapping',str(mapping),
                                 '--require-quality-review'])
            self.assertEqual(code,3)
            self.assertEqual(json.loads(stdout.getvalue())['status'],'needs_review')

    def test_cli_rejects_nonfinite_thresholds(self):
        root=Path(__file__).resolve().parents[1]/'assets/examples/zh-en/target.en.srt'
        for option,value in [('--max-cps','nan'),('--gap-seconds','inf'),('--duration-seconds','inf')]:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(audit.main(['audit',str(root),option,value]),2)

    def test_json_report_cannot_overwrite_input_or_existing_file(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-cli-test-') as tmp:
            src=Path(tmp)/'source.srt'
            src.write_text('1\n00:00:01,000 --> 00:00:03,000\nHello\n',encoding='utf-8')
            original=src.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(audit.main(['audit',str(src),'--json',str(src)]),2)
            self.assertEqual(src.read_bytes(),original)
            report=Path(tmp)/'report.json'
            report.write_text('keep',encoding='utf-8')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(audit.main(['audit',str(src),'--json',str(report)]),2)
            self.assertEqual(report.read_text(encoding='utf-8'),'keep')


if __name__=='__main__':
    unittest.main()
