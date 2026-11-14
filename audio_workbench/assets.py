"""Immutable PCM sources; archival affects new arrangements, not past renders."""
import hashlib
import time
import uuid
from . import waveio
from .catalog import identifier, display_name


class AssetLibrary:
    def __init__(self, catalog):
        self.catalog = catalog

    def import_audio(self, name, wav_bytes):
        name = display_name(name)
        if not isinstance(wav_bytes, bytes) or len(wav_bytes) > 6 * 1024 * 1024:
            raise ValueError('asset must be WAV bytes up to 6 MiB')
        audio = waveio.decode(wav_bytes)
        digest = hashlib.sha256(wav_bytes).hexdigest()
        asset_id = uuid.uuid4().hex
        path = self.catalog.root / 'blobs' / (digest + '.wav')
        # Identical imports may share a blob; metadata and archival stay independent.
        self.catalog.atomic_write(path, wav_bytes)
        with self.catalog.connect() as db:
            db.execute('INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)',
                       (asset_id, name, digest, audio.rate, audio.channels, audio.frames,
                        len(wav_bytes), 0, time.time()))
        return self.get(asset_id)

    def get(self, asset_id, allow_archived=True):
        identifier(asset_id, 'asset id')
        with self.catalog.connect() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
        if row is None:
            raise LookupError('asset not found')
        result = dict(row)
        result['archived'] = bool(result['archived'])
        if result['archived'] and not allow_archived:
            raise ValueError('archived assets cannot be used in new revisions')
        return result

    def resolve(self, asset_id):
        return self.get(asset_id, allow_archived=False)

    def content(self, asset_id, expected_sha256=None):
        asset = self.get(asset_id)
        if expected_sha256 is not None and expected_sha256 != asset['sha256']:
            raise ValueError('asset differs from the frozen render')
        try:
            data = (self.catalog.root / 'blobs' / (asset['sha256'] + '.wav')).read_bytes()
        except FileNotFoundError as exc:
            raise ValueError('asset content is missing') from exc
        if len(data) != asset['size'] or hashlib.sha256(data).hexdigest() != asset['sha256']:
            raise ValueError('asset integrity check failed')
        return data

    def list(self, include_archived=False):
        with self.catalog.connect() as db:
            rows = db.execute('SELECT * FROM assets WHERE archived=0 OR ? ORDER BY created,id',
                              (bool(include_archived),)).fetchall()
        return [dict(row, archived=bool(row['archived'])) for row in rows]

    def archive(self, asset_id):
        identifier(asset_id, 'asset id')
        with self.catalog.connect() as db:
            if not db.execute('UPDATE assets SET archived=1 WHERE id=?', (asset_id,)).rowcount:
                raise LookupError('asset not found')
        return self.get(asset_id)
