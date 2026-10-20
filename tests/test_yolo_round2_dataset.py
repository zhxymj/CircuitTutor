"""Negative cases for the round-two acquisition and source isolation contract."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import prepare_yolo_round2 as module
from PIL import Image


class RoundTwoTests(unittest.TestCase):
    def test_train_shortfall_is_retained_with_actual_count(self):
        result = module.quota_result(12, 1)
        self.assertEqual(result, {'drafter': 12, 'planned_additions': 2,
                                  'available_additions': 1, 'shortfall': 1})

    def test_test_drafter_still_requires_all_twelve(self):
        with self.assertRaisesRegex(ValueError, '12'):
            module.quota_result(31, 11)

    def test_wrong_effective_coordinate_dimensions_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'image.jpg'
            Image.new('RGB', (40, 20)).save(path)
            with self.assertRaisesRegex(ValueError, 'dimensions'):
                module.base.image_geometry(path, (20, 40))

    def test_split_reused_drawing_refused(self):
        samples = [dict(id='a', drafter=1, split='train', source_C=1, source_D=1,
                        source_photo=1, source_image_sha256='a'),
                   dict(id='b', drafter=31, split='test', source_C=1, source_D=1,
                        source_photo=2, source_image_sha256='b')]
        with self.assertRaisesRegex(ValueError, 'circuit|drawing'):
            module.validate_isolation(samples)

    def test_cross_split_image_hash_refused(self):
        samples = [dict(id='a', drafter=1, split='train', source_C=1, source_D=1,
                        source_photo=1, source_image_sha256='same'),
                   dict(id='b', drafter=31, split='test', source_C=361, source_D=1,
                        source_photo=1, source_image_sha256='same')]
        with self.assertRaisesRegex(ValueError, 'hash'):
            module.validate_isolation(samples)

    def test_budget_rejected_before_request(self):
        budget = module.NetworkBudget(10)
        budget.reserve(6)
        with self.assertRaisesRegex(ValueError, 'budget'):
            budget.reserve(5)
        self.assertEqual(budget.requested_bytes, 6)

    def test_corrupt_member_crc_refused(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('source.xml', b'original data')
        raw = stream.getvalue().replace(b'original data', b'corrupt! data')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            with self.assertRaises(zipfile.BadZipFile):
                module.read_member(archive, 'source.xml')


if __name__ == '__main__':
    unittest.main()
