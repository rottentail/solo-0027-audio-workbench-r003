import array
import copy
import hashlib
import random
import unittest

from audio_workbench.arrangement import compile_document
from audio_workbench.processing import clip, conform, saturate
from audio_workbench.rendering import RenderCancelled, render_manifest
from audio_workbench.waveio import Audio, encode


class RenderingTests(unittest.TestCase):
    def make_assets(self, sources):
        self.payloads = {key: encode(audio) for key, audio in sources.items()}
        self.metadata = {key: {'sample_rate': audio.rate, 'channels': audio.channels,
                               'frames': audio.frames, 'sha256': hashlib.sha256(self.payloads[key]).hexdigest()}
                         for key, audio in sources.items()}

    def read(self, key, digest):
        self.assertEqual(digest, self.metadata[key]['sha256'])
        return self.payloads[key]

    def plan(self, clips, rate=16000, channels=2, master_gain=1):
        return compile_document({'schema_version': 1, 'format': {'sample_rate': rate, 'channels': channels},
                                 'master_gain': master_gain, 'clips': clips}, self.metadata.__getitem__)

    def test_matches_legacy_pcm_stages_across_formats(self):
        rng = random.Random(849)
        for source_channels in (1, 2):
            for output_channels in (1, 2):
                for output_rate in (12000, 16000, 44100):
                    a = Audio(12000, source_channels, array.array('h',
                        [rng.randint(-32768, 32767) for _ in range(47 * source_channels)]))
                    b = Audio(8000, 1, array.array('h', [rng.randint(-32768, 32767) for _ in range(29)]))
                    self.make_assets({'a': a, 'b': b})
                    clips = [{'id': 'a', 'asset_id': 'a', 'start': 3 / 12000, 'end': 37 / 12000,
                              'gain': .63, 'at': .001},
                             {'id': 'b', 'asset_id': 'b', 'gain': 1.7}]
                    plan = self.plan(clips, output_rate, output_channels, .37)
                    result = render_manifest(plan, self.read)
                    aa = conform(clip(a, 3 / 12000, 37 / 12000, 1), output_rate, output_channels)
                    bb = conform(b, output_rate, output_channels)
                    offset = round(.001 * output_rate) * output_channels
                    sums = [0.0] * (plan['frames'] * output_channels)
                    for index, sample in enumerate(aa.samples):
                        sums[offset + index] += sample * .63
                    for index, sample in enumerate(bb.samples):
                        sums[index] += sample * 1.7
                    expected = Audio(output_rate, output_channels,
                                     array.array('h', [saturate(value * .37) for value in sums]))
                    with self.subTest(source=source_channels, output=output_channels, rate=output_rate):
                        self.assertEqual((result.rate, result.channels, list(result.samples)),
                                         (expected.rate, expected.channels, list(expected.samples)))

    def test_offset_clip_keeps_source_continuity_past_resampler_window(self):
        # Clips longer than 2048 output frames must keep advancing through the
        # trimmed source; a timeline offset may not restart the source window.
        rng = random.Random(2048)
        source = Audio(8000, 1, array.array('h', [rng.randint(-32768, 32767) for _ in range(5000)]))
        self.make_assets({'a': source})
        expected = conform(source, 16000, 1)
        rendered = {}
        for at in (0, .5):
            plan = self.plan([{'id': 'a', 'asset_id': 'a', 'at': at}], rate=16000, channels=1)
            self.assertEqual(plan['clips'][0]['output_frames'], 10000)
            result = render_manifest(plan, self.read)
            offset = round(at * 16000)
            rendered[at] = list(result.samples[offset:offset + len(expected.samples)])
            self.assertEqual(result.frames, offset + len(expected.samples))
            if offset:
                self.assertEqual(set(result.samples[:offset]), {0})
        self.assertEqual(rendered[.5], list(expected.samples))
        self.assertEqual(rendered[0], rendered[.5])
        self.assertNotEqual(list(expected.samples[2048:2056]), list(expected.samples[:8]))

    def test_long_trimmed_multitrack_mix_matches_legacy_stages(self):
        rng = random.Random(4096)
        sources = {
            'wide': Audio(12000, 2, array.array('h', [rng.randint(-32768, 32767) for _ in range(6000 * 2)])),
            'mono': Audio(8000, 1, array.array('h', [rng.randint(-32768, 32767) for _ in range(3000)])),
        }
        self.make_assets(sources)
        clips = [{'id': 'wide', 'asset_id': 'wide', 'start': 5 / 12000, 'end': 5995 / 12000,
                  'at': .25, 'gain': .8},
                 {'id': 'mono', 'asset_id': 'mono', 'at': .1, 'gain': 1.3}]
        for output_rate, output_channels in ((12000, 1), (16000, 2), (22050, 1), (44100, 2)):
            plan = self.plan(clips, output_rate, output_channels, .6)
            result = render_manifest(plan, self.read)
            sums = [0.0] * (plan['frames'] * output_channels)
            for clip_spec, source in zip(plan['clips'], sources.values()):
                first = clip_spec['source_start'] / source.rate
                last = (clip_spec['source_start'] + clip_spec['source_frames']) / source.rate
                converted = conform(clip(source, first, last, 1), output_rate, output_channels)
                offset = clip_spec['at_frame'] * output_channels
                for index, sample in enumerate(converted.samples):
                    sums[offset + index] += sample * clip_spec['gain']
            expected = [saturate(value * .6) for value in sums]
            with self.subTest(rate=output_rate, channels=output_channels):
                self.assertEqual(list(result.samples), expected)

    def test_short_clip_at_offset_uses_trimmed_source_frames(self):
        rng = random.Random(12)
        source = Audio(16000, 2, array.array('h', [rng.randint(-32768, 32767) for _ in range(4000 * 2)]))
        self.make_assets({'a': source})
        clips = [{'id': 'a', 'asset_id': 'a', 'start': 3997 / 16000, 'end': 4000 / 16000, 'at': .75}]
        plan = self.plan(clips, rate=48000, channels=2)
        result = render_manifest(plan, self.read)
        expected = conform(clip(source, 3997 / 16000, 4000 / 16000, 1), 48000, 2)
        offset = round(.75 * 48000) * 2
        self.assertEqual(result.frames, round(.75 * 48000) + len(expected.samples) // 2)
        self.assertEqual(list(result.samples[offset:offset + len(expected.samples)]),
                         list(expected.samples))

    def test_master_gain_applies_to_sum_before_final_saturation(self):
        self.make_assets({'a': Audio(8000, 1, array.array('h', [30000, -30000]))})
        plan = self.plan([{'id': 'a', 'asset_id': 'a'}, {'id': 'b', 'asset_id': 'a'}],
                         rate=8000, channels=1, master_gain=.5)
        self.assertEqual(list(render_manifest(plan, self.read).samples), [30000, -30000])

    def test_muted_tail_keeps_silent_extent_and_progress_is_monotonic(self):
        self.make_assets({'a': Audio(8000, 1, array.array('h', [100, 200]))})
        plan = self.plan([{'id': 'sound', 'asset_id': 'a'},
                          {'id': 'tail', 'asset_id': 'a', 'at': 1, 'muted': True}],
                         rate=8000, channels=1)
        progress = []
        result = render_manifest(plan, self.read, progress=progress.append)
        self.assertEqual(result.frames, 8002)
        self.assertEqual(list(result.samples[:2]), [100, 200])
        self.assertEqual(set(result.samples[2:]), {0})
        self.assertEqual(progress[0], 0)
        self.assertEqual(progress[-1], 1)
        self.assertEqual(progress, sorted(progress))

    def test_rejects_modified_content_and_inconsistent_source_facts(self):
        self.make_assets({'a': Audio(8000, 1, array.array('h', [100, 200]))})
        plan = self.plan([{'id': 'a', 'asset_id': 'a'}])
        altered = encode(Audio(8000, 1, array.array('h', [101, 200])))
        with self.assertRaisesRegex(ValueError, 'digest'):
            render_manifest(plan, lambda key, digest: altered)
        damaged = copy.deepcopy(plan)
        damaged['clips'][0]['source_start'] = 1
        with self.assertRaisesRegex(ValueError, 'format or interval'):
            render_manifest(damaged, self.read)
        damaged = copy.deepcopy(plan)
        damaged['clips'][0]['source_channels'] = 2
        with self.assertRaisesRegex(ValueError, 'format or interval'):
            render_manifest(damaged, self.read)

    def test_cancellation_before_read_and_during_long_pcm_render(self):
        self.make_assets({'a': Audio(8000, 1, array.array('h', [123] * 16000))})
        plan = self.plan([{'id': 'a', 'asset_id': 'a'}], rate=48000)
        with self.assertRaises(RenderCancelled):
            render_manifest(plan, lambda *args: self.fail('must not read cancelled source'), cancelled=lambda: True)
        state = {'cancelled': False}
        progress = []

        def report(value):
            progress.append(value)
            if value > .1:
                state['cancelled'] = True

        with self.assertRaises(RenderCancelled):
            render_manifest(plan, self.read, cancelled=lambda: state['cancelled'], progress=report)
        self.assertLess(progress[-1], 1)


if __name__ == '__main__':
    unittest.main()
