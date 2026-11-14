import array
import io
import sys
import wave
from dataclasses import dataclass


@dataclass
class Audio:
    rate: int
    channels: int
    samples: array.array

    @property
    def frames(self):
        return len(self.samples) // self.channels


def decode(data):
    try:
        with wave.open(io.BytesIO(data), 'rb') as reader:
            channels, width, rate, frames = (reader.getnchannels(), reader.getsampwidth(),
                                             reader.getframerate(), reader.getnframes())
            if channels not in (1, 2) or width != 2 or not 8000 <= rate <= 48000:
                raise ValueError('expected 16-bit PCM, 1 or 2 channels, 8000-48000 Hz')
            if frames < 1 or frames > rate * 30:
                raise ValueError('audio duration must be greater than zero and at most 30 seconds')
            raw = reader.readframes(frames)
            if len(raw) != frames * channels * width:
                raise ValueError('truncated audio data')
    except (wave.Error, EOFError) as exc:
        raise ValueError('invalid WAV data') from exc
    samples = array.array('h')
    samples.frombytes(raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    return Audio(rate, channels, samples)


def encode(audio):
    output = io.BytesIO()
    samples = array.array('h', audio.samples)
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(audio.channels)
        writer.setsampwidth(2)
        writer.setframerate(audio.rate)
        writer.writeframes(samples.tobytes())
    return output.getvalue()
