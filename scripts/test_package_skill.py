import json
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile

import package_skill as package


class PackageTests(unittest.TestCase):
    def rewrite(self, original, destination, mutate):
        with zipfile.ZipFile(original) as source:
            items = [(name,source.read(name)) for name in source.namelist()]
        with zipfile.ZipFile(destination,'w') as target:
            for name,raw in mutate(items):
                target.writestr(name,raw)

    def test_package_round_trip_and_repeatable_bytes(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-package-test-') as tmp:
            first,second = Path(tmp)/'one.zip',Path(tmp)/'two.zip'
            result = package.build(first)
            package.build(second)
            self.assertEqual(result['status'],'package_verified')
            self.assertEqual(first.read_bytes(),second.read_bytes())
            self.assertEqual(result['files'],len(package.RELEASE_FILES))

    def test_build_does_not_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-package-test-') as tmp:
            output=Path(tmp)/'keep.zip'
            output.write_bytes(b'keep')
            with self.assertRaises(FileExistsError): package.build(output)
            self.assertEqual(output.read_bytes(),b'keep')

    def test_extra_path_and_traversal_are_rejected_without_extraction(self):
        for extra in ('subtitle-workflow/private.txt','../outside.txt'):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory(prefix='subtitle-package-test-') as tmp:
                src,dst=Path(tmp)/'good.zip',Path(tmp)/'bad.zip'
                package.build(src)
                self.rewrite(src,dst,lambda items:items+[(extra,b'private')])
                with self.assertRaises(ValueError): package.verify(dst)
                self.assertFalse((Path(tmp)/'outside.txt').exists())

    def test_altered_payload_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-package-test-') as tmp:
            src,dst=Path(tmp)/'good.zip',Path(tmp)/'bad.zip'
            package.build(src)
            self.rewrite(src,dst,lambda items:[(n,b'changed' if n.endswith('/SKILL.md') else r) for n,r in items])
            with self.assertRaisesRegex(ValueError,'fingerprint mismatch'): package.verify(dst)

    def test_duplicate_zip_member_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-package-test-') as tmp:
            src,dst=Path(tmp)/'good.zip',Path(tmp)/'bad.zip'
            package.build(src)
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                self.rewrite(src,dst,lambda items:items+[items[0]])
            with self.assertRaises(ValueError): package.verify(dst)

    def test_incomplete_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-package-test-') as tmp:
            src,dst=Path(tmp)/'good.zip',Path(tmp)/'bad.zip'
            package.build(src)
            def mutate(items):
                result=[]
                for name,raw in items:
                    if name==package.MANIFEST_NAME:
                        data=json.loads(raw)
                        data['files'].pop()
                        raw=json.dumps(data).encode('utf-8')
                    result.append((name,raw))
                return result
            self.rewrite(src,dst,mutate)
            with self.assertRaises(ValueError): package.verify(dst)


if __name__=='__main__':
    unittest.main()
