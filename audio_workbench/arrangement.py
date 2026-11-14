"""Validate editable arrangements and freeze their sample-level render plans."""

import math
import re


MAX_CLIPS = 32
MAX_SECONDS = 60


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f'{name} must be an object')
    return value


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer between {low} and {high}')
    return value


def _number(value, name, low, high):
    # Check range before isfinite so enormous JSON integers cannot overflow float.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a finite number between {low} and {high}')
    if not low <= value <= high or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number between {low} and {high}')
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f'{name} must be a nonempty string of at most 128 characters')
    return value


def _format(rate, channels):
    _integer(rate, 'sample_rate', 8000, 48000)
    _integer(channels, 'channels', 1, 2)


def _source(metadata):
    _object(metadata, 'asset metadata')
    rate = metadata.get('sample_rate')
    channels = metadata.get('channels')
    _format(rate, channels)
    frames = _integer(metadata.get('frames'), 'asset frames', 1, rate * 30)
    digest = metadata.get('sha256')
    if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
        raise ValueError('asset sha256 must be a lowercase hexadecimal digest')
    return rate, channels, frames, digest


def _prepare(document, resolve_asset):
    _object(document, 'document')
    if type(document.get('schema_version')) is not int or document['schema_version'] != 1:
        raise ValueError('unsupported arrangement schema_version')
    format_ = _object(document.get('format'), 'format')
    rate, channels = format_.get('sample_rate'), format_.get('channels')
    _format(rate, channels)
    master_gain = _number(document.get('master_gain', 1), 'master_gain', 0, 4)
    clips = document.get('clips')
    if not isinstance(clips, list) or not 1 <= len(clips) <= MAX_CLIPS:
        raise ValueError(f'clips must contain between 1 and {MAX_CLIPS} entries')
    normalized, frozen, seen = [], [], set()
    for clip in clips:
        _object(clip, 'clip')
        clip_id = _identifier(clip.get('id'), 'clip id')
        if clip_id in seen:
            raise ValueError('clip ids must be unique')
        seen.add(clip_id)
        asset_id = _identifier(clip.get('asset_id'), 'asset_id')
        source_rate, source_channels, source_total, digest = _source(resolve_asset(asset_id))
        if rate < source_rate:
            raise ValueError('arrangement sample rate cannot downsample an asset')
        duration = source_total / source_rate
        start = _number(clip.get('start', 0), 'start', 0, duration)
        end = _number(clip.get('end', duration), 'end', 0, duration)
        at = _number(clip.get('at', 0), 'at', 0, MAX_SECONDS)
        gain = _number(clip.get('gain', 1), 'gain', 0, 4)
        muted = clip.get('muted', False)
        if type(muted) is not bool:
            raise ValueError('muted must be a boolean')
        first, last = round(start * source_rate), round(end * source_rate)
        if start >= end or first >= last or last > source_total:
            raise ValueError('clip interval must contain frames within the asset')
        output_frames = round((last - first) * rate / source_rate)
        at_frame = round(at * rate)
        if output_frames < 1 or at_frame + output_frames > rate * MAX_SECONDS:
            raise ValueError('arrangement duration must be at most 60 seconds')
        normalized.append({'id': clip_id, 'asset_id': asset_id, 'start': start,
                           'end': end, 'at': at, 'gain': gain, 'muted': muted})
        frozen.append({'clip_id': clip_id, 'asset_id': asset_id, 'sha256': digest,
                       'source_rate': source_rate, 'source_channels': source_channels,
                       'source_start': first, 'source_frames': last - first,
                       'at_frame': at_frame, 'output_frames': output_frames,
                       'gain': gain, 'muted': muted})
    canonical = {'schema_version': 1, 'format': {'sample_rate': rate, 'channels': channels},
                 'master_gain': master_gain, 'clips': normalized}
    manifest = {'schema_version': 1, 'engine': 'pcm-v1', 'sample_rate': rate,
                'channels': channels, 'frames': max(c['at_frame'] + c['output_frames'] for c in frozen),
                'master_gain': master_gain, 'clips': frozen}
    return canonical, manifest


def normalize_document(document, resolve_asset):
    """Return a fresh canonical editable document, resolving every source."""
    return _prepare(document, resolve_asset)[0]


def compile_document(document, resolve_asset):
    """Resolve an editable revision into an immutable, path-free PCM plan."""
    return _prepare(document, resolve_asset)[1]


def validate_manifest(manifest):
    """Validate persisted render plans without consulting mutable project state."""
    _object(manifest, 'manifest')
    if (type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
            or manifest.get('engine') != 'pcm-v1'):
        raise ValueError('unsupported render manifest schema or engine')
    rate, channels = manifest.get('sample_rate'), manifest.get('channels')
    _format(rate, channels)
    frames = _integer(manifest.get('frames'), 'frames', 1, rate * MAX_SECONDS)
    _number(manifest.get('master_gain'), 'master_gain', 0, 4)
    clips = manifest.get('clips')
    if not isinstance(clips, list) or not 1 <= len(clips) <= MAX_CLIPS:
        raise ValueError('invalid manifest clips')
    seen, extent = set(), 0
    for clip in clips:
        _object(clip, 'manifest clip')
        clip_id = _identifier(clip.get('clip_id'), 'clip_id')
        if clip_id in seen:
            raise ValueError('manifest clip ids must be unique')
        seen.add(clip_id)
        _identifier(clip.get('asset_id'), 'asset_id')
        source_rate, _, _, _ = _source({
            'sample_rate': clip.get('source_rate'), 'channels': clip.get('source_channels'),
            'frames': clip.get('source_frames'), 'sha256': clip.get('sha256')})
        if source_rate > rate:
            raise ValueError('manifest cannot downsample an asset')
        first = _integer(clip.get('source_start'), 'source_start', 0, source_rate * 30 - 1)
        count = clip['source_frames']
        if first + count > source_rate * 30:
            raise ValueError('source interval exceeds maximum asset duration')
        at = _integer(clip.get('at_frame'), 'at_frame', 0, rate * MAX_SECONDS - 1)
        output = _integer(clip.get('output_frames'), 'output_frames', 1, rate * 30)
        if output != round(count * rate / source_rate):
            raise ValueError('output_frames disagrees with source interval')
        if at + output > frames:
            raise ValueError('clip extends beyond manifest frames')
        _number(clip.get('gain'), 'gain', 0, 4)
        if type(clip.get('muted')) is not bool:
            raise ValueError('muted must be a boolean')
        extent = max(extent, at + output)
    if extent != frames:
        raise ValueError('manifest frames must equal the clip timeline extent')
    return manifest
