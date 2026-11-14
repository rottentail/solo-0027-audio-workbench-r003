"""Construct constrained FFmpeg commands for supported output formats."""
import imageio_ffmpeg

PRESETS = {
    'mp4': ['-vf', 'scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2', '-r', '30', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac'],
    'audio': ['-vn', '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le'],
}


def command(source, destination, options):
    if options['preset'] == 'timeline':
        return timeline_command(source.parent, destination, options['segments'])
    fmt = 'mp4' if options['preset'] == 'mp4' else 'wav'
    return [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-ss', str(options['start']), '-i', str(source), '-t', str(options['duration']),
            *PRESETS[options['preset']], '-f', fmt, '-y', str(destination)]


def timeline_command(folder, destination, segments):
    args = [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin']
    filters = []
    inputs = []
    for index, segment in enumerate(segments):
        args += ['-i', str(folder / f'source-{index}')]
        start, duration = segment['start'], segment['duration']
        filters.append(
            f'[{index}:v:0]trim=start={start}:duration={duration},setpts=PTS-STARTPTS,'
            f'fps=30,scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2,'
            f'pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,'
            f'tpad=stop_mode=clone:stop_duration={duration},trim=duration={duration},setpts=PTS-STARTPTS[v{index}]')
        filters.append(
            f'[{index}:a:0]atrim=start={start}:duration={duration},asetpts=PTS-STARTPTS,'
            f'aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,'
            f'apad,atrim=duration={duration},asetpts=PTS-STARTPTS[a{index}]')
        inputs.append(f'[v{index}][a{index}]')
    filters.append(''.join(inputs) + f'concat=n={len(segments)}:v=1:a=1[vout][aout]')
    return args + ['-filter_complex', ';'.join(filters), '-map', '[vout]', '-map', '[aout]',
                   '-r', '30', '-fps_mode', 'cfr', '-c:v', 'libx264', '-preset', 'ultrafast',
                   '-c:a', 'aac', '-f', 'mp4', '-y', str(destination)]
