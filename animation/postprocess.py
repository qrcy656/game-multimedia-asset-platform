"""CPU gray-background sprite preparation; no semantic segmentation model.

Requires ffmpeg/ffprobe on PATH and Pillow >=10.4, numpy >=1.26, scipy >=1.11.
The color key is deliberately specialized: connected border colors can also
occur on a character, so every result requires visual review.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage


ALGORITHM = 'border-connected-color-distance-v1'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _options(options: dict) -> dict:
    result = {'fps': 12.0, 'frame_width': 512, 'frame_height': 512,
              'anchor': 'feet_center', 'background': [128, 128, 128],
              'background_tolerance': 12.0, 'matte_softness': 4.0,
              'remove_background': True, 'align_anchor': True,
              'frame_selection': 'uniform', 'selected_indices': None,
              'max_frames': 120, 'columns': 8, 'min_component_area': 16,
              'deduplicate_threshold': 1.0, 'padding_fraction': 0.04,
              'resample': 'nearest'}
    if not isinstance(options, dict):
        raise ValueError('options must be a dictionary')
    unknown = set(options) - set(result)
    if unknown:
        raise ValueError('Unknown postprocess options: ' + ', '.join(sorted(unknown)))
    result.update(options)
    background = result['background']
    if isinstance(background, dict):
        result['background'] = background.get('rgb', background.get('color', [128, 128, 128]))
        result['background_tolerance'] = background.get('tolerance', result['background_tolerance'])
    if isinstance(result['background'], (int, float)):
        result['background'] = [result['background']] * 3
    rgb = result['background']
    if not isinstance(rgb, (tuple, list)) or len(rgb) != 3 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 255
            for v in rgb):
        raise ValueError('background must contain three finite RGB values in 0..255')
    result['background'] = list(map(float, rgb))
    for name, low, high in (('fps', 1, 60), ('background_tolerance', 1, 100),
                            ('matte_softness', 1, 60), ('deduplicate_threshold', 0, 30),
                            ('padding_fraction', 0, 0.15)):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{name} must be finite and between {low} and {high}')
    for name, low, high in (('frame_width', 16, 2048), ('frame_height', 16, 2048),
                            ('max_frames', 1, 512), ('columns', 1, 32), ('min_component_area', 1, 4096)):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{name} must be an integer between {low} and {high}')
    for name in ('remove_background', 'align_anchor'):
        if not isinstance(result[name], bool):
            raise ValueError(name + ' must be a boolean')
    if result['anchor'] != 'feet_center':
        raise ValueError('Only the feet_center anchor is implemented')
    if result['frame_selection'] not in ('uniform', 'deduplicate'):
        raise ValueError('frame_selection must be uniform or deduplicate')
    if result['resample'] not in ('nearest', 'bilinear'):
        raise ValueError('resample must be nearest or bilinear')
    if result['frame_width'] * result['frame_height'] * result['max_frames'] > 134_217_728:
        raise ValueError('Requested frame canvases exceed the 128 megapixel memory budget; lower max_frames or dimensions')
    indices = result['selected_indices']
    if indices is not None and (not isinstance(indices, list) or not indices or len(indices) > result['max_frames']
                                or any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < 4096 for i in indices)
                                or len(set(indices)) != len(indices)):
        raise ValueError('selected_indices must contain distinct nonnegative integers within max_frames')
    return result


def validate_options(options: dict) -> dict:
    """Validate without decoding, so an API can reject options before generation.

    The returned dictionary contains defaults and normalized background fields.
    Source-dependent checks, including available selected indices, run later.
    """
    return _options(options)


def _run(argv: list[str], timeout: int = 180) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{Path(argv[0]).name} exited {result.returncode}: {result.stderr[-6000:]}')
    return result.stdout


def _probe(video_path: Path) -> dict:
    metadata = json.loads(_run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                               '-show_streams', '-show_format', '-of', 'json', str(video_path)]))
    streams = metadata.get('streams', [])
    if not streams:
        raise ValueError('Source has no video stream')
    stream = streams[0]
    width, height = int(stream['width']), int(stream['height'])
    duration = float(stream.get('duration') or metadata.get('format', {}).get('duration', 0))
    if not (math.isfinite(duration) and 0 < duration <= 120):
        raise ValueError('Source must have a finite duration of at most 120 seconds')
    if not (1 <= width <= 4096 and 1 <= height <= 4096 and width * height <= 8_388_608):
        raise ValueError('Source dimensions exceed the CPU processing limit')
    # The frame timestamps make source_index exact, including variable-rate input.
    frames = json.loads(_run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                             '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time',
                             '-of', 'json', str(video_path)]))['frames']
    if not frames or len(frames) > 30_000:
        raise ValueError('Source must contain 1..30000 decodable frames')
    timestamps = np.array([float(frame['best_effort_timestamp_time']) for frame in frames], dtype=np.float64)
    if not np.all(np.isfinite(timestamps)) or np.any(np.diff(timestamps) < 0):
        raise ValueError('Source has invalid or non-monotonic frame timestamps')
    timestamps -= timestamps[0]
    return {'width': width, 'height': height, 'duration_seconds': duration,
            'source_frame_count': len(frames), 'average_frame_rate': stream.get('avg_frame_rate'),
            'codec': stream.get('codec_name'), 'timestamps': timestamps.tolist()}


def _sampling(source: dict, fps: float) -> list[dict]:
    timestamps = np.asarray(source['timestamps'])
    count = int(math.ceil(source['duration_seconds'] * fps - 1e-7))
    if not 1 <= count <= 4096:
        raise ValueError('Target sampling requires more than 4096 frames; reduce fps or source duration')
    result = []
    previous = -1
    for sample_index in range(count):
        target_time = sample_index / fps
        right = min(int(np.searchsorted(timestamps, target_time)), len(timestamps) - 1)
        left = max(0, right - 1)
        index = left if abs(timestamps[left] - target_time) <= abs(timestamps[right] - target_time) else right
        if index == previous:
            continue  # A higher requested fps cannot add source motion information.
        result.append({'sample_index': len(result), 'source_index': index,
                       'source_time_seconds': float(timestamps[index]),
                       'sample_time_seconds': target_time})
        previous = index
    return result


def _extract(video_path: Path, work: Path, samples: list[dict]) -> list[Path]:
    filters = work / 'select-filter.txt'
    # The filter is generated from integer indices, never from user text. A file
    # avoids command-line length limits for a longer animation.
    filters.write_text('select=' + '+'.join(f'eq(n\\,{sample["source_index"]})' for sample in samples), encoding='ascii')
    target = work / 'decoded'
    target.mkdir()
    _run(['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-i', str(video_path),
          '-map', '0:v:0', '-an', '-filter_script:v', str(filters), '-vsync', '0',
          '-frames:v', str(len(samples)), '-pix_fmt', 'rgba', '-start_number', '0',
          str(target / 'frame_%06d.png')])
    paths = sorted(target.glob('frame_*.png'))
    if len(paths) != len(samples):
        raise RuntimeError(f'FFmpeg extracted {len(paths)} frames; expected {len(samples)}')
    return paths


def _select(paths: list[Path], options: dict) -> tuple[list[int], dict]:
    explicit = options['selected_indices']
    if explicit is not None:
        if (not isinstance(explicit, list) or not explicit or len(explicit) > options['max_frames']
                or any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(paths) for i in explicit)
                or len(set(explicit)) != len(explicit)):
            raise ValueError('selected_indices must be a nonempty list of distinct sampled-frame indices within max_frames')
        return explicit[:], {'method': 'explicit', 'removed_consecutive_duplicates': 0}
    candidates = list(range(len(paths)))
    removed = 0
    if options['frame_selection'] == 'deduplicate':
        candidates = []
        previous = None
        for index, path in enumerate(paths):
            with Image.open(path) as image:
                thumb = np.asarray(image.convert('RGB').resize((64, 64), Image.Resampling.BILINEAR), dtype=np.float32)
            if previous is None or float(np.mean(np.abs(thumb - previous))) > options['deduplicate_threshold']:
                candidates.append(index)
                previous = thumb
            else:
                removed += 1
    if len(candidates) > options['max_frames']:
        candidates = [candidates[i] for i in np.linspace(0, len(candidates) - 1, options['max_frames'], dtype=int)]
    return candidates, {'method': options['frame_selection'], 'removed_consecutive_duplicates': removed,
                        'deduplicate_threshold_rgb_mae': options['deduplicate_threshold'],
                        'comparison': 'consecutive retained frame, 64x64 RGB thumbnail'}


def _bbox(alpha: np.ndarray) -> list[int]:
    ys, xs = np.nonzero(alpha > 8)
    if len(xs) == 0:
        raise ValueError('Matte has no foreground; review background color and tolerance')
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def _matte_image(image: Image.Image, options: dict) -> tuple[Image.Image, dict]:
    pixels = np.array(image.convert('RGBA'))
    rgb = pixels[:, :, :3].astype(np.float32)
    alpha = pixels[:, :, 3].copy()
    warnings = []
    estimate = None
    background_fraction = 0.0
    collision_fraction = 0.0
    if options['remove_background']:
        nominal = np.asarray(options['background'], dtype=np.float32)
        border = np.concatenate((rgb[0], rgb[-1], rgb[1:-1, 0], rgb[1:-1, -1]))
        plausible = np.linalg.norm(border - nominal, axis=1) <= max(64.0, options['background_tolerance'] * 2)
        if np.mean(plausible) < 0.25:
            raise ValueError('No reliable border background matches the requested color; manual review or a different matte is required')
        estimate = np.median(border[plausible], axis=0)
        distance = np.linalg.norm(rgb - estimate, axis=2)
        candidate = distance <= options['background_tolerance'] + options['matte_softness']
        seeds = np.zeros(candidate.shape, dtype=bool)
        seeds[0] = candidate[0]
        seeds[-1] = candidate[-1]
        seeds[:, 0] = candidate[:, 0]
        seeds[:, -1] = candidate[:, -1]
        connected = ndimage.binary_propagation(seeds, structure=np.ones((3, 3)), mask=candidate)
        background_fraction = float(np.mean(connected))
        if background_fraction < 0.01:
            raise ValueError('No useful boundary-connected background was found; refusing opaque sprite output')
        soft = np.clip((distance - options['background_tolerance']) / options['matte_softness'], 0, 1)
        keyed = np.where(connected, np.round(soft * 255), 255).astype(np.uint8)
        alpha = np.minimum(alpha, keyed)
        foreground = alpha >= 128
        collision_fraction = float(np.sum(foreground & (distance <= options['background_tolerance'])) / max(1, np.sum(foreground)))
        if collision_fraction > 0.01:
            warnings.append('Foreground contains background-like colors; enclosed regions were retained, but exposed matching details may be lost.')
        if np.linalg.norm(estimate - nominal) > options['background_tolerance']:
            warnings.append('Estimated border color differs substantially from the configured background color.')
        # Suppress gray fringes only where the estimated matte is partially
        # transparent. This color unmixing is heuristic, not learned matting.
        edge = (alpha > 0) & (alpha < 255)
        a = np.maximum(alpha.astype(np.float32) / 255.0, 1 / 255)[:, :, None]
        unmixed = np.clip((rgb - (1 - a) * estimate) / a, 0, 255)
        pixels[:, :, :3][edge] = np.round(unmixed[edge]).astype(np.uint8)
    if np.all(alpha == 255):
        raise ValueError('No alpha transparency was produced; refusing an opaque sprite sequence')
    if not np.any(alpha >= 128):
        raise ValueError('Matte contains no opaque foreground')
    labels, count = ndimage.label(alpha > 8, structure=np.ones((3, 3)))
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    small = sizes < options['min_component_area']
    small[0] = False
    removed_components = int(np.sum(small))
    removed_pixels = int(np.sum(sizes[small]))
    alpha[small[labels]] = 0
    pixels[:, :, 3] = alpha
    pixels[alpha == 0, :3] = 0
    bbox = _bbox(alpha)
    if bbox[0] == 0 or bbox[1] == 0 or bbox[2] == image.width or bbox[3] == image.height:
        warnings.append('Foreground touches a source boundary; the source may contain a cropped character.')
    return Image.fromarray(pixels), {'bbox': bbox, 'estimated_background_rgb': estimate.tolist() if estimate is not None else None,
                                    'boundary_background_fraction': background_fraction,
                                    'foreground_color_collision_fraction': collision_fraction,
                                    'removed_components': removed_components, 'removed_noise_pixels': removed_pixels,
                                    'warnings': warnings}


def _feet_center(image: Image.Image) -> list[float]:
    alpha = np.asarray(image)[:, :, 3]
    ys, xs = np.nonzero(alpha >= 128)
    if not len(xs):
        raise ValueError('Cannot estimate feet without an opaque foreground')
    # The low quantile rejects isolated bottom pixels. The lowest six percent
    # of the body supplies a robust horizontal support span, not the coat bbox.
    bottom = float(np.quantile(ys, 0.999)) + 1.0
    band = max(2.0, (float(ys.max()) - float(ys.min()) + 1) * 0.06)
    support = xs[ys >= bottom - band]
    return [float((np.quantile(support, 0.10) + np.quantile(support, 0.90)) / 2 + 0.5), bottom]


def _layout(records: list[dict], options: dict, source_size: tuple[int, int]) -> tuple[float, list[float]]:
    width, height = options['frame_width'], options['frame_height']
    target = [width / 2.0, float(round(height * 0.9))]
    padding = min(width, height) * options['padding_fraction']
    if not options['align_anchor']:
        return min((width - 2 * padding) / source_size[0], (height - 2 * padding) / source_size[1]), target
    left = max(record['source_anchor'][0] - record['bbox'][0] for record in records)
    right = max(record['bbox'][2] - record['source_anchor'][0] for record in records)
    above = max(record['source_anchor'][1] - record['bbox'][1] for record in records)
    below = max(record['bbox'][3] - record['source_anchor'][1] for record in records)
    factors = [(target[0] - padding, left), (width - padding - target[0], right),
               (target[1] - padding, above), (height - padding - target[1], below)]
    scale = min(available / extent for available, extent in factors if extent > 0)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('Cannot fit foreground around the target anchor')
    return scale, target


def _loop_metrics(first: Image.Image, last: Image.Image) -> dict:
    a, b = np.asarray(first, dtype=np.float32) / 255, np.asarray(last, dtype=np.float32) / 255
    mask_a, mask_b = a[:, :, 3] >= 0.5, b[:, :, 3] >= 0.5
    union = mask_a | mask_b
    rgb_a, rgb_b = a[:, :, :3] * a[:, :, 3:4], b[:, :, :3] * b[:, :, 3:4]
    return {'first_last_alpha_iou': float(np.sum(mask_a & mask_b) / max(1, np.sum(union))),
            'first_last_premultiplied_rgb_mae': float(np.mean(np.abs(rgb_a[union] - rgb_b[union]))) if np.any(union) else 0.0,
            'first_last_alpha_mae': float(np.mean(np.abs(a[:, :, 3] - b[:, :, 3]))),
            'seamless_loop_verified': False,
            'interpretation': 'Endpoint appearance only; this does not measure velocity continuity or guarantee a seamless loop.'}


def _build(video_path: Path, destination: Path, work: Path, options: dict, matte_provider=None) -> dict:
    source = _probe(video_path)
    samples = _sampling(source, options['fps'])
    paths = _extract(video_path, work, samples)
    selected, selection = _select(paths, options)
    model_mattes = matte_provider([paths[index] for index in selected], work / 'api-mattes') if matte_provider else None
    if model_mattes and len(model_mattes['paths']) != len(selected):
        raise ValueError('Background model did not return every selected frame')
    frames_dir, matte_dir = destination / 'frames', work / 'mattes'
    frames_dir.mkdir()
    matte_dir.mkdir()
    records = []
    source_size = None
    for order, sample_index in enumerate(selected):
        with Image.open(model_mattes['paths'][order] if model_mattes else paths[sample_index]) as image:
            if source_size is not None and image.size != source_size:
                raise ValueError('Decoded frame dimensions changed within the sequence')
            source_size = image.size
            # Model frames already contain native alpha. Retain the existing
            # nonempty-alpha/noise validation, without keying subject colors.
            matte, detail = _matte_image(image, {**options, 'remove_background': False} if model_mattes else options)
        anchor = _feet_center(matte)
        matte.save(matte_dir / f'{order:06d}.png')
        records.append({**samples[sample_index], **detail, 'source_anchor': anchor})
    scale, target_anchor = _layout(records, options, source_size)
    width, height = options['frame_width'], options['frame_height']
    columns = min(options['columns'], len(records))
    rows = math.ceil(len(records) / columns)
    if width * columns * height * rows > 150_994_944:
        raise ValueError('Sprite sheet exceeds the memory budget; reduce dimensions or frame count')
    sheet = Image.new('RGBA', (columns * width, rows * height))
    previews = []
    durations = []
    elapsed_seconds = 0.0
    elapsed_milliseconds = 0
    sprite_frames = []
    resample = Image.Resampling.NEAREST if options['resample'] == 'nearest' else Image.Resampling.BILINEAR
    for order, record in enumerate(records):
        if options['align_anchor']:
            offset = [target_anchor[i] - record['source_anchor'][i] * scale for i in (0, 1)]
        else:
            offset = [(width - source_size[0] * scale) / 2, (height - source_size[1] * scale) / 2]
        with Image.open(matte_dir / f'{order:06d}.png') as matte:
            frame = matte.transform((width, height), Image.Transform.AFFINE,
                                    (1 / scale, 0, -offset[0] / scale, 0, 1 / scale, -offset[1] / scale),
                                    resample=resample, fillcolor=(0, 0, 0, 0))
        filename = f'frame_{order:04d}.png'
        frame_path = frames_dir / filename
        frame.save(frame_path)
        if selection['method'] == 'explicit':
            duration = 1 / options['fps']
        else:
            end = records[order + 1]['sample_time_seconds'] if order + 1 < len(records) else source['duration_seconds']
            duration = max(0.001, end - record['sample_time_seconds'])
        elapsed_seconds += duration
        quantized_end = int(round(elapsed_seconds * 1000))
        duration_ms = max(1, quantized_end - elapsed_milliseconds)
        elapsed_milliseconds += duration_ms
        durations.append(duration_ms)
        x, y = (order % columns) * width, (order // columns) * height
        sheet.paste(frame, (x, y))
        anchor = [record['source_anchor'][i] * scale + offset[i] for i in (0, 1)]
        sprite_frame = {'file': 'frames/' + filename, 'rect': {'x': x, 'y': y, 'w': width, 'h': height},
                        'anchor': {'x': anchor[0], 'y': anchor[1]}, 'source_index': record['source_index'],
                        'sample_index': record['sample_index'], 'source_time_seconds': record['source_time_seconds'],
                        'duration_ms': duration_ms, 'duration_seconds': duration,
                        'bbox': _bbox(np.asarray(frame)[:, :, 3]), 'sha256': _sha256(frame_path)}
        record.update({'output': sprite_frame, 'uniform_scale': scale, 'translation': offset})
        sprite_frames.append(sprite_frame)
        previews.append(frame)
    sheet.save(destination / 'spritesheet.png')
    # Lossless animated WebP preserves soft alpha, unlike a GIF's one-bit mask.
    previews[0].save(destination / 'preview.webp', save_all=True, append_images=previews[1:],
                     duration=durations, loop=0, lossless=True, method=4, exact=True)
    loop = _loop_metrics(previews[0], previews[-1])
    warnings = [('API matte alpha and subject consistency require visual review.' if model_mattes else
                 'Gray-background color key requires visual review; exposed subject colors matching the background may be removed.'),
                'Feet position is estimated from the lowest opaque support pixels; flying poses, skirts, shadows, or occlusion can invalidate it.',
                'Loop endpoint metrics do not establish seamless motion or a valid running direction.']
    warnings += sorted({warning for record in records for warning in record['warnings']})
    if loop['first_last_alpha_iou'] < 0.90 or loop['first_last_premultiplied_rgb_mae'] > 0.08:
        warnings.append('First and last frames differ visibly by endpoint metrics; inspect the loop transition.')
    quality = {'visual_review_required': True, 'visual_acceptance': 'not_reviewed',
               'seamless_loop_verified': False, 'loop': loop, 'warnings': warnings,
               'source_cropped': any('source boundary' in warning for warning in warnings)}
    sprite_metadata = {'image': 'spritesheet.png', 'frame_width': width, 'frame_height': height,
                       'columns': columns, 'rows': rows, 'frame_count': len(records),
                       'uniform_scale': scale, 'anchor_mode': options['anchor'],
                       'anchor_aligned': options['align_anchor'], 'frames': sprite_frames,
                       'coordinate_convention': 'Top-left origin; bbox right/bottom are exclusive; anchor coordinates are in frame pixels.'}
    _write_json(destination / 'spritesheet.json', sprite_metadata)
    source_dir = destination / 'source'
    source_dir.mkdir()
    source_copy = source_dir / ('video' + (video_path.suffix.lower() or '.bin'))
    shutil.copy2(video_path, source_copy)
    artifacts = {'frames_dir': 'frames', 'spritesheet': 'spritesheet.png',
                 'spritesheet_json': 'spritesheet.json', 'preview': 'preview.webp',
                 'manifest': 'manifest.json', 'source_video': source_copy.relative_to(destination).as_posix()}
    manifest = {'schema_version': 1, 'algorithm': 'api-matte-rgba-v1' if model_mattes else ALGORITHM, 'parameters': options,
                'source': {key: value for key, value in source.items() if key != 'timestamps'},
                'source_sha256': _sha256(video_path), 'source_preserved': True,
                'source_index_definition': 'Zero-based decoded source frame index from ffprobe presentation timestamps.',
                'selected_indices_definition': 'Zero-based frame after target-fps sampling; explicit order is honored at 1/fps duration.',
                'selection': selection, 'sampled_frame_count': len(samples), 'frame_count': len(records),
                'decoded_size': list(source_size), 'uniform_scale': scale,
                'fixed_anchor': target_anchor if options['align_anchor'] else None,
                'quality': quality, 'artifacts': artifacts, 'frames': records,
                'artifact_sha256': {name: _sha256(destination / artifacts[name])
                                    for name in ('spritesheet', 'spritesheet_json', 'preview', 'source_video')},
                'alpha_algorithm': 'Estimate border RGB median near configured color; flood only boundary-connected color candidates; '
                                   'soft distance matte, local color unmixing, remove small connected components.',
                'scale_algorithm': 'One scale fits all selected foreground extents around one fixed output anchor; no per-frame zoom.',
                'preview_note': 'WebP encoders may coalesce identical frames; PNG sequence and spritesheet JSON are authoritative.'}
    _write_json(destination / 'manifest.json', manifest)
    if model_mattes:
        manifest['background_model'] = model_mattes['metadata']
        manifest['alpha_algorithm'] = 'API matte native RGBA background removal; alpha validation and small-component cleanup.'
        _write_json(destination / 'manifest.json', manifest)
    return {**artifacts, 'frame_count': len(records), 'quality': quality,
            **({'background_model': model_mattes['metadata']} if model_mattes else {})}


def process_video(video_path: Path, output_dir: Path, options: dict, *, matte_provider=None) -> dict:
    """Prepare transparent sprites in a new/empty directory and return relative paths.

    ``selected_indices`` indexes the target-fps samples, not the original video;
    its explicit order is honored with fixed 1/fps duration. Uniform selection
    and consecutive deduplication preserve sampled elapsed time via durations.
    Existing outputs are never overwritten. The source video is copied intact.
    """
    video_path, output_dir = Path(video_path).resolve(), Path(output_dir).absolute()
    parameters = validate_options(options)
    if not video_path.is_file():
        raise ValueError('Source video does not exist: ' + str(video_path))
    if output_dir.is_symlink() or (output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir()))):
        raise ValueError('Output directory must be new or empty; existing results will not be overwritten')
    for program in ('ffmpeg', 'ffprobe'):
        if shutil.which(program) is None:
            raise RuntimeError(program + ' is required on PATH for CPU video processing')
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sprite-processing-', dir=output_dir.parent) as temporary:
        staging = Path(temporary)
        destination, work = staging / 'artifacts', staging / 'work'
        destination.mkdir()
        work.mkdir()
        result = _build(video_path, destination, work, parameters, matte_provider)
        if output_dir.exists():
            output_dir.rmdir()  # Fails safely if another process added a file.
        os.rename(destination, output_dir)
    return result
