import copy
import unittest

from audio_workbench.arrangement import compile_document, normalize_document, validate_manifest


class ArrangementTests(unittest.TestCase):
    def setUp(self):
        self.assets = {
            'voice': {'sample_rate': 8000, 'channels': 1, 'frames': 8, 'sha256': 'a' * 64},
            'stereo': {'sample_rate': 12000, 'channels': 2, 'frames': 12, 'sha256': 'b' * 64},
        }
        self.document = {'schema_version': 1, 'format': {'sample_rate': 16000, 'channels': 2},
                         'clips': [{'id': 'spoken', 'asset_id': 'voice'}]}

    def resolve(self, asset_id):
        return self.assets[asset_id]

    def test_defaults_produce_detached_editable_document(self):
        original = copy.deepcopy(self.document)
        normalized = normalize_document(self.document, self.resolve)
        self.assertEqual(self.document, original)
        self.assertEqual(normalized['master_gain'], 1)
        self.assertEqual(normalized['clips'][0], {
            'id': 'spoken', 'asset_id': 'voice', 'start': 0, 'end': .001,
            'at': 0, 'gain': 1, 'muted': False})
        normalized['clips'][0]['gain'] = 2
        self.assertNotIn('gain', self.document['clips'][0])

    def test_trim_quantization_target_extent_and_muted_tail(self):
        self.document['clips'] = [
            {'id': 'first', 'asset_id': 'voice', 'start': 1.5 / 8000, 'end': 6.5 / 8000},
            {'id': 'tail', 'asset_id': 'stereo', 'at': 1, 'muted': True}]
        manifest = compile_document(self.document, self.resolve)
        self.assertEqual(manifest['clips'][0]['source_start'], 2)
        self.assertEqual(manifest['clips'][0]['source_frames'], 4)
        self.assertEqual(manifest['clips'][0]['output_frames'], 8)
        self.assertEqual(manifest['frames'], 16016)
        self.assertEqual(manifest['clips'][1]['sha256'], 'b' * 64)
        self.assertIs(validate_manifest(manifest), manifest)

    def test_rejects_invalid_numeric_and_boolean_inputs(self):
        mutations = [
            lambda d: d.update(schema_version=True),
            lambda d: d['format'].update(sample_rate=True),
            lambda d: d['format'].update(channels=1.0),
            lambda d: d.update(master_gain=float('nan')),
            lambda d: d.update(master_gain=10 ** 400),
            lambda d: d['clips'][0].update(start=True),
            lambda d: d['clips'][0].update(end=float('inf')),
            lambda d: d['clips'][0].update(at=-.1),
            lambda d: d['clips'][0].update(gain=4.01),
            lambda d: d['clips'][0].update(muted=1),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                document = copy.deepcopy(self.document)
                mutation(document)
                with self.assertRaises(ValueError):
                    normalize_document(document, self.resolve)

    def test_rejects_no_samples_duplicates_downsampling_and_overlong_extent(self):
        variants = []
        d = copy.deepcopy(self.document)
        d['clips'][0].update(start=0, end=.4 / 8000)
        variants.append(d)
        d = copy.deepcopy(self.document)
        d['clips'].append(copy.deepcopy(d['clips'][0]))
        variants.append(d)
        d = copy.deepcopy(self.document)
        d['format']['sample_rate'] = 8000
        d['clips'][0]['asset_id'] = 'stereo'
        variants.append(d)
        d = copy.deepcopy(self.document)
        d['clips'][0]['at'] = 60
        variants.append(d)
        d = copy.deepcopy(self.document)
        d['clips'][0]['end'] = .00101
        variants.append(d)
        d = copy.deepcopy(self.document)
        d['clips'] = []
        variants.append(d)
        for document in variants:
            with self.subTest(document=document), self.assertRaises(ValueError):
                compile_document(document, self.resolve)

    def test_frozen_plan_rejects_structural_corruption(self):
        manifest = compile_document(self.document, self.resolve)
        mutations = [
            lambda m: m.update(engine='future'),
            lambda m: m.update(frames=m['frames'] + 1),
            lambda m: m['clips'][0].update(output_frames=3),
            lambda m: m['clips'][0].update(source_start=-1),
            lambda m: m['clips'][0].update(source_frames=True),
            lambda m: m['clips'][0].update(sha256='../local.wav'),
            lambda m: m['clips'][0].update(muted=None),
        ]
        for mutation in mutations:
            damaged = copy.deepcopy(manifest)
            mutation(damaged)
            with self.subTest(damaged=damaged), self.assertRaises(ValueError):
                validate_manifest(damaged)


if __name__ == '__main__':
    unittest.main()
