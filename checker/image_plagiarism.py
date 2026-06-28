"""
Image plagiarism detection via multi-crop perceptual hashing.

Strategy: for each image we compute pHash at three scales:
full image, 82 % center crop, 65 % center crop.
When comparing two images we take the minimum Hamming distance across all
9 hash-pair combinations. This makes the detector robust to students who
crop the borders of a screenshot (10-35 % edge removal).

Hash size 12 gives a 144-bit hash; MAX_HAMMING 17 is about 12 % tolerance.

Historical reports supply pre-computed hashes + thumbnails so we never need
to reload the original PIL images from prior sessions.
"""

import base64
import io

import imagehash
from PIL import Image

HASH_SIZE   = 12        # 12 × 12 = 144-bit pHash
MAX_HAMMING = 17        # ≤ 17/144 ≈ 12 % bit-difference → "same image"
CROP_SCALES = (1.0, 0.82, 0.65)
THUMB_SIZE  = (200, 160)


def _center_crop(img: Image.Image, scale: float) -> Image.Image:
    w, h = img.size
    hw = max(int(w * scale / 2), 10)
    hh = max(int(h * scale / 2), 10)
    cx, cy = w // 2, h // 2
    return img.crop((
        max(0, cx - hw), max(0, cy - hh),
        min(w, cx + hw), min(h, cy + hh),
    ))


def _compute_hashes(img: Image.Image) -> list:
    """Return [pHash, pHash, pHash] for full image + two center crops."""
    hashes = []
    for scale in CROP_SCALES:
        cropped = _center_crop(img, scale) if scale < 1.0 else img
        hashes.append(imagehash.phash(cropped, hash_size=HASH_SIZE))
    return hashes


def _min_distance(hashes_a: list, hashes_b: list) -> int:
    """Minimum Hamming distance across all 9 hash-pair combinations."""
    return min(ha - hb for ha in hashes_a for hb in hashes_b)


def _to_b64(img: Image.Image) -> str:
    thumb = img.copy()
    thumb.thumbnail(THUMB_SIZE, Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format='JPEG', quality=75)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()


def check_image_plagiarism(reports: list) -> dict:
    """
    Find identical / near-identical / cropped-copy images across reports.

    Accepts a mix of regular reports and historical virtual reports.
    Historical reports carry 'precomputed_images' (hashes + thumbnails) instead
    of 'images' (PIL objects).  Historical-vs-historical pairs are skipped.

    Returns:
        pairs, list of dicts with thumbnail data URIs and match info
    """
    all_imgs = []

    def _sk(r: dict) -> str:
        s = r.get('student', {})
        k = f"{s.get('name','').strip().lower()}|{s.get('group','').strip().lower()}"
        return k if k != '|' else ''

    for r in reports:
        is_hist   = r.get('is_historical', False)
        sk        = _sk(r)

        if is_hist:
            for img_info in r.get('precomputed_images', []):
                all_imgs.append({
                    'report':      r['path'],
                    'page':        img_info['page'],
                    'hashes':      img_info['hashes'],
                    'thumb':       img_info.get('thumb'),
                    'is_hist':     True,
                    'student_key': sk,
                })
        else:
            for img_info in r.get('images', []):
                pil = img_info['pil']
                try:
                    hashes = _compute_hashes(pil)
                    all_imgs.append({
                        'report':      r['path'],
                        'page':        img_info['page'],
                        'hashes':      hashes,
                        'thumb':       None,
                        'pil':         pil,
                        'is_hist':     False,
                        'student_key': sk,
                    })
                except Exception:
                    pass

    pairs = []
    seen: set = set()

    for i in range(len(all_imgs)):
        for j in range(i + 1, len(all_imgs)):
            a, b = all_imgs[i], all_imgs[j]

            if a['report'] == b['report']:
                continue

            if a['is_hist'] and b['is_hist']:
                continue

            # Skip: same student (same name + group)
            if a['student_key'] and a['student_key'] == b['student_key']:
                continue

            key = tuple(sorted([(a['report'], a['page']), (b['report'], b['page'])]))
            if key in seen:
                continue

            dist = _min_distance(a['hashes'], b['hashes'])
            if dist > MAX_HAMMING:
                continue

            seen.add(key)

            exact_dist = a['hashes'][0] - b['hashes'][0]
            is_crop = (exact_dist > MAX_HAMMING) and (dist <= MAX_HAMMING)

            def _thumb(entry):
                if entry.get('thumb'):
                    return entry['thumb']
                if entry.get('pil'):
                    return _to_b64(entry['pil'])
                return ''

            pairs.append({
                'report1':  a['report'],
                'page1':    a['page'],
                'img1':     _thumb(a),
                'report2':  b['report'],
                'page2':    b['page'],
                'img2':     _thumb(b),
                'distance': dist,
                'is_crop':  is_crop,
            })

    pairs.sort(key=lambda x: x['distance'])
    return {'pairs': pairs}
