"""Render frozen arrangements using the legacy PCM stage-rounding contract."""

import array
import hashlib

from .arrangement import validate_manifest
from .processing import saturate
from .waveio import Audio, decode


class RenderCancelled(Exception):
    """The render was stopped before it could publish an output."""


def render_manifest(manifest, read_asset, cancelled=lambda: False, progress=lambda value: None):
    """Render a frozen plan; callbacks never consult an editable project head."""
    validate_manifest(manifest)
    rate, channels = manifest['sample_rate'], manifest['channels']
    frames = manifest['frames']
    total_work = sum(c['output_frames'] for c in manifest['clips']) + frames
    completed = 0

    def checkpoint():
        if cancelled():
            raise RenderCancelled('render cancelled')
        progress(completed / total_work)

    checkpoint()
    sums = [0.0] * (frames * channels)
    for clip in manifest['clips']:
        checkpoint()
        data = read_asset(clip['asset_id'], clip['sha256'])
        if hashlib.sha256(data).hexdigest() != clip['sha256']:
            raise ValueError('asset content digest does not match frozen render')
        source = decode(data)
        if (source.rate != clip['source_rate'] or source.channels != clip['source_channels']
                or clip['source_start'] + clip['source_frames'] > source.frames):
            raise ValueError('asset format or interval does not match frozen render')
        checkpoint()
        count = clip['source_frames']
        output_frames = clip['output_frames']
        if clip['muted'] or clip['gain'] == 0:
            completed += output_frames
            checkpoint()
            continue
        # Interpolate inside the trimmed source, clamping at its final frame,
        # exactly as processing.clip(..., gain=1) followed by conform does.
        for frame in range(output_frames):
            if frame % 2048 == 0:
                checkpoint()
            source_frame = frame
            position = source_frame * source.rate / rate
            first = min(int(position), count - 1)
            second = min(first + 1, count - 1)
            fraction = position - first
            values = []
            for channel in range(source.channels):
                left = source.samples[(clip['source_start'] + first) * source.channels + channel]
                right = source.samples[(clip['source_start'] + second) * source.channels + channel]
                values.append(left + (right - left) * fraction)
            if channels == 1:
                converted = (saturate(sum(values) / len(values)),)
            elif source.channels == 1:
                converted = (saturate(values[0]), saturate(values[0]))
            else:
                converted = tuple(saturate(value) for value in values)
            offset = (clip['at_frame'] + frame) * channels
            for channel, sample in enumerate(converted):
                sums[offset + channel] += sample * clip['gain']
            completed += 1
    samples = array.array('h')
    for frame in range(frames):
        if frame % 2048 == 0:
            checkpoint()
        offset = frame * channels
        samples.extend(saturate(sums[offset + channel] * manifest['master_gain'])
                       for channel in range(channels))
        completed += 1
    checkpoint()
    return Audio(rate, channels, samples)
