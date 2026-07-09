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
from PIL import Image, ImageOps

HASH_SIZE   = 12        # 12 × 12 = 144-bit pHash
MAX_HAMMING = 17        # ≤ 17/144 ≈ 12 % bit-difference → "same image"
CROP_SCALES = (1.0, 0.82, 0.65)
THUMB_SIZE  = (200, 160)

# UI screenshots (Zabbix, terminal, IDE) of different students look alike by
# design, so they get a stricter bar: only the full-image hashes are compared
# (no multi-crop minimum) and a copy is declared at ≤ UI_MAX_HAMMING. Pairs
# between UI_MAX_HAMMING and MAX_HAMMING are reported as "похожий интерфейс,
# проверьте вручную" instead of a copy.
UI_MAX_HAMMING = 6


def _is_ui_like(img: Image.Image) -> bool:
    """Heuristic screenshot detector: flat dominant background / few colors.

    Photos and scans have thousands of distinct colors and no dominant one;
    interface screenshots are mostly one background color drawn with a small
    palette.
    """
    small = img.convert('RGB').resize((64, 64))
    small = ImageOps.posterize(small, 4)   # drop JPEG noise in low bits
    colors = small.getcolors(64 * 64) or []
    if not colors:
        return False
    total = 64 * 64
    dominant_share = max(n for n, _ in colors) / total
    unique_share = len(colors) / total
    return dominant_share >= 0.35 or unique_share <= 0.05


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


# pHash normalizes to a 48px grid, so hashing a downscaled copy of a huge
# image gives the same result while avoiding multi-megapixel crops.
MAX_META_PIXELS = 4_000_000


def image_meta(pil_img: Image.Image) -> dict:
    """Everything the pipeline needs from one image: hashes, thumbnail,
    UI flag. Computed once at extraction time so the caller can drop the
    PIL object immediately — keeping big batches within RAM."""
    img = pil_img
    if img.width * img.height > MAX_META_PIXELS:
        img = img.copy()
        img.thumbnail((2048, 2048), Image.LANCZOS)
    return {
        'w':      pil_img.width,
        'h':      pil_img.height,
        'hashes': _compute_hashes(img),
        'thumb':  _to_b64(img),
        'is_ui':  _is_ui_like(img),
    }


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
                    'is_ui':       img_info.get('is_ui', False),
                    'student_key': sk,
                })
        else:
            for img_info in r.get('images', []):
                if not img_info.get('hashes'):
                    continue
                all_imgs.append({
                    'report':      r['path'],
                    'page':        img_info.get('page', 0),
                    'hashes':      img_info['hashes'],
                    'thumb':       img_info.get('thumb'),
                    'is_hist':     False,
                    'is_ui':       img_info.get('is_ui', False),
                    'student_key': sk,
                })

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

            pair_is_ui = bool(a.get('is_ui') or b.get('is_ui'))

            if pair_is_ui:
                # Full-image hashes only: crops of a shared UI match trivially
                # and would drown the report in false positives.
                dist = a['hashes'][0] - b['hashes'][0]
                if dist > MAX_HAMMING:
                    continue
                is_crop = False
                ui_review = dist > UI_MAX_HAMMING
            else:
                dist = _min_distance(a['hashes'], b['hashes'])
                if dist > MAX_HAMMING:
                    continue
                exact_dist = a['hashes'][0] - b['hashes'][0]
                is_crop = (exact_dist > MAX_HAMMING) and (dist <= MAX_HAMMING)
                ui_review = False

            seen.add(key)

            def _thumb(entry):
                return entry.get('thumb') or ''

            pairs.append({
                'report1':  a['report'],
                'page1':    a['page'],
                'img1':     _thumb(a),
                'report2':  b['report'],
                'page2':    b['page'],
                'img2':     _thumb(b),
                'distance':  dist,
                'is_crop':   is_crop,
                'is_ui':     pair_is_ui,
                'ui_review': ui_review,
            })

    pairs.sort(key=lambda x: x['distance'])
    return {'pairs': pairs}
