"""Source-based trim/composition/captions/audio in one final video encode."""
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

from app.config import settings
from app.core.ffprobe import get_video_metadata
from app.core.media import ffmpeg
from app.core.recipes import EditRecipe

RENDERER_VERSION = 'source-render-v1'


def _ass_time(seconds):
    centiseconds = round(seconds*100)
    return f'{centiseconds//360000}:{centiseconds//6000%60:02d}:{centiseconds//100%60:02d}.{centiseconds%100:02d}'


def _ass_color(value):
    r, g, b = value[1:3], value[3:5], value[5:7]
    return f'&H00{b}{g}{r}'


def recipe_ass(recipe):
    from app.core.captions import ASS_HEADER
    style = recipe.captions.style
    header = ASS_HEADER.format(playres_x=1080, playres_y=1920, font=style.font, size=style.font_size,
                               primary=_ass_color(style.color), secondary=_ass_color(style.color),
                               outline_col='&H00000000', back='&H80000000', bold=-1,
                               outline=4, shadow=0, align=2, margin_v=style.margin_bottom)
    events = []
    if recipe.captions.enabled:
        for word in recipe.captions.words:
            # User/model text is literal. Full-width braces/backslash prevent ASS
            # control injection without discarding visible punctuation.
            text = word.word.translate(str.maketrans({'{': '｛', '}': '｝', '\\': '＼', '\n': ' ', '\r': ' '}))
            text = ''.join(c for c in text if c.isprintable())
            events.append(f'Dialogue: 0,{_ass_time(word.start)},{_ass_time(word.end)},Default,,0,0,0,,'
                          f'{{\\c{_ass_color(style.highlight_color)}}}{text}')
    return header + '\n'.join(events) + '\n'


def _position(keys, axis, default, scale):
    points = [(0.0, default)]
    for key in keys:
        if key.time == 0:
            points[0] = (0.0, getattr(key, axis))
        else:
            points.append((key.time, getattr(key, axis)))
    expression = f'{points[-1][1]*scale:.6f}'
    for (t0, p0), (t1, p1) in reversed(list(zip(points, points[1:]))):
        value = f'{p0*scale:.6f}+{(p1-p0)*scale:.6f}*(t-{t0:.6f})/{t1-t0:.6f}'
        expression = f'if(lt(t,{t1:.6f}),{value},{expression})'
    return expression


def _crop(region, width, height, keys=()):
    w, h = max(2, int(region.width*width)//2*2), max(2, int(region.height*height)//2*2)
    x = _position(keys, 'x', region.x, width)
    y = _position(keys, 'y', region.y, height)
    return f"crop={w}:{h}:x='clip({x},0,iw-{w})':y='clip({y},0,ih-{h})'"


def _fit(width, height, background):
    return (f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x{background[1:]},setsar=1')


def build_command(recipe, source_path, metadata, kind, output):
    width, height = (1080, 1920) if kind == 'final' else (360, 640)
    rate = Fraction(recipe.export.frame_rate)
    if kind == 'preview':
        rate = min(rate, Fraction(30))
    rate_string = f'{rate.numerator}/{rate.denominator}'
    # Recipes address displayed source coordinates, including non-square pixels.
    sar_text = metadata.get('sample_aspect_ratio') or '1:1'
    sar = Fraction(sar_text.replace(':', '/')) if sar_text not in ('N/A', '0:1') else Fraction(1)
    display_width = max(2, int(metadata['width']*sar)//2*2)
    source_height = metadata['height']
    graph, inputs = [], []
    for n, interval in enumerate(recipe.intervals):
        duration = interval.end-interval.start
        inputs += ['-ss', str(interval.start), '-t', str(duration), '-i', str(Path(source_path).resolve())]
        graph.append(f'[{n}:{metadata["video_stream_index"]}]trim=duration={duration},'
                     f'setpts=PTS-STARTPTS,scale={display_width}:{source_height},setsar=1,settb=AVTB[v{n}]')
        tracks = []
        for j, track in enumerate(recipe.audio.tracks):
            label = f'a{n}_{j}'
            graph.append(f'[{n}:{track.stream_index}]atrim=start=0:end={duration},'
                         f'aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo,'
                         f'volume={track.gain},apad,atrim=duration={duration}[{label}]')
            tracks.append(f'[{label}]')
        if not tracks:
            graph.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={duration},asetpts=PTS-STARTPTS[a{n}]')
        else:
            graph.append(''.join(tracks) + f'amix=inputs={len(tracks)}:normalize=0:duration=longest,'
                         f'alimiter=limit=0.95:level=false:latency=true[a{n}]')
    pairs = ''.join(f'[v{n}][a{n}]' for n in range(len(recipe.intervals)))
    graph.append(pairs + f'concat=n={len(recipe.intervals)}:v=1:a=1[video][audio]')
    layout = recipe.layout
    if layout.mode == 'gameplay_facecam':
        panel = int(height*layout.facecam_height)//2*2
        graph.append('[video]split=2[gamein][facein]')
        graph.append(f'[gamein]{_crop(layout.gameplay, display_width, source_height)},'
                     f'{_fit(width, height-panel, layout.background)}[game]')
        graph.append(f'[facein]{_crop(layout.facecam, display_width, source_height)},'
                     f'scale={width}:{panel}:force_original_aspect_ratio=increase,'
                     f'crop={width}:{panel},setsar=1[face]')
        graph.append('[face][game]vstack=inputs=2[composition]')
    else:
        crop = _crop(layout.gameplay, display_width, source_height, layout.crop_keyframes)
        composition = (_fit(width, height, layout.background) if layout.mode == 'fit_background' else
                       f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1')
        graph.append(f'[video]{crop},{composition}[composition]')
    subtitles = ',ass=filename=captions.ass' if recipe.captions.enabled and recipe.captions.words else ''
    graph.append(f'[composition]fps={rate_string}{subtitles},format=yuv420p[vout]')
    command = [ffmpeg(), '-y', '-v', 'error', *inputs, '-filter_complex_threads', '1',
               '-filter_complex', ';'.join(graph), '-map', '[vout]', '-map', '[audio]',
               '-t', str(recipe.duration), '-c:v', 'libx264', '-preset', recipe.export.preset,
               '-crf', str(recipe.export.crf if kind == 'final' else 27), '-pix_fmt', 'yuv420p',
               '-r', rate_string, '-fps_mode', 'cfr', '-c:a', 'aac', '-ar', '48000', '-ac', '2',
               '-b:a', '192k', '-movflags', '+faststart', str(Path(output).resolve())]
    return command


def validate_output(path, recipe, kind):
    meta = get_video_metadata(str(path))
    expected_size = (1080, 1920) if kind == 'final' else (360, 640)
    rate = Fraction(recipe.export.frame_rate)
    if kind == 'preview':
        rate = min(rate, Fraction(30))
    tolerance = max(0.15, 2/float(rate))
    if (meta['width'], meta['height']) != expected_size or meta['codec'] != 'h264':
        raise ValueError('Render codec or dimensions do not match the preset')
    if abs(meta['duration']-recipe.duration) > tolerance:
        raise ValueError('Rendered duration does not match the recipe')
    if abs(float(Fraction(meta['frame_rate']))-float(rate)) > 0.001:
        raise ValueError('Rendered frame rate does not match the preset')
    if meta['sample_aspect_ratio'] != '1:1' or meta['color']['pix_fmt'] != 'yuv420p':
        raise ValueError('Rendered pixel/sample aspect format does not match the preset')
    audio = next((s for s in meta['streams'] if s['codec_type'] == 'audio'), None)
    video = next(s for s in meta['streams'] if s['codec_type'] == 'video')
    if audio is None or audio.get('codec_name') != 'aac':
        raise ValueError('Expected AAC audio stream is missing')
    starts = [float(s.get('start_time', 0)) for s in (video, audio)]
    ends = [start+float(s.get('duration', meta['duration'])) for start, s in zip(starts, (video, audio))]
    if abs(starts[0]-starts[1]) > tolerance or abs(ends[0]-ends[1]) > tolerance:
        raise ValueError('Rendered audio/video duration alignment failed')
    return meta


def render_recipe(recipe, source_path, metadata, kind, output_path):
    recipe = EditRecipe.model_validate(recipe)
    if kind not in ('preview', 'final'):
        raise ValueError('Unknown render kind')
    if metadata['color'].get('color_transfer') in ('smpte2084', 'arib-std-b67'):
        raise ValueError('HDR input requires the tone-mapping preset planned for phase 6')
    if any(abs(float(d.get('rotation', 0))) > 0.1 for s in metadata['streams']
           if s['codec_type'] == 'video' for d in s.get('side_data_list', [])):
        raise ValueError('Rotated input requires a normalized source orientation')
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        folder = Path(temporary)
        (folder/'captions.ass').write_text(recipe_ass(recipe))
        target = folder/'render.mp4'
        command = build_command(recipe, source_path, metadata, kind, target)
        try:
            subprocess.run(command, check=True, capture_output=True, text=True,
                           timeout=settings.media_timeout_seconds, cwd=folder)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f'Source render failed: {exc.stderr[-4000:]}') from exc
        verified = validate_output(target, recipe, kind)
        # Preserve recipe-local ASS beside each immutable render for inspection.
        (folder/'captions.ass').replace(output.with_suffix('.ass'))
        target.replace(output)
    return verified
