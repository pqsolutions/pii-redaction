"""
Lightweight, format-specific redaction helpers for the web app's fast,
synchronous operations: previewing a redacted output file, adding one missed
PII value ("targeted re-mask"), and editing/deleting a manually-added value.
See wiki/feature-updates/phase-1/plan-table-implementation.md.

The masking/unmasking logic here is deliberately DUPLICATED from (not
imported from) dual_redaction_pipeline.py: that module eagerly imports
Docling, GLiNER, Presidio, and cv2 at module load, which would drag
multi-second, GB-scale model loading into the always-running Flask process
just to reuse a handful of pure-format functions -- defeating the point of a
fast add-one-value flow. This module only needs openpyxl / xlrd / xlwt /
python-docx / pymupdf / PIL / pywin32, so app.py can import it directly.
dual_redaction_pipeline.py itself is left untouched to avoid any regression
risk to the production pipeline it already implements.
"""

import base64
import io
import re
import time
from collections import OrderedDict
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont, ImageOps

import crypto_utils
import email_utils

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
EMAIL_EXTENSIONS = {".eml", ".msg"}
MAX_IMG_SIDE = 2500

# OCR is expensive and not perfectly deterministic across runs (a scanned
# page can be transcribed slightly differently each time Tesseract sees it).
# Preview rendering and single-value masking both extract words from the
# same on-disk file independently; without sharing results, a value the
# marquee tool read from the preview's OCR pass can fail to be found by the
# masking function's own, separate OCR pass. These caches key on file
# identity (path + mtime + size, which changes on every add/edit/delete since
# those rewrite the file in place) so preview and mask always see the same
# words for a given file state, and stale entries simply age out of the
# bounded LRU as files change.
_PDF_WORDS_CACHE_MAX = 1000
_PDF_WORDS_CACHE = OrderedDict()  # (path, mtime_ns, size, page_num, use_ocr_fallback) -> (words, is_ocr)

_IMAGE_WORDS_CACHE_MAX = 300
_IMAGE_WORDS_CACHE = OrderedDict()  # (path, mtime_ns, size) -> words

TOKEN_RE = re.compile(r"<\*([A-Z0-9_]+?)_(\d+)\*>")


def next_token_for_category(existing_tokens, category: str) -> str:
    """Mint the next free token for `category` given the tokens already used
    by one file (e.g. existing ["<*PERSON_1*>", "<*PERSON_2*>"] + category
    "PERSON" -> "<*PERSON_3*>"). Mirrors get_candidate_token()'s token format
    (dual_redaction_pipeline.py:1516) but reconstructs the counter from
    persisted rows instead of an in-memory dict, since a manual add has no
    live pipeline run to carry that state."""
    cat = re.sub(r"[^A-Za-z0-9]+", "_", category.strip()).strip("_").upper() or "VALUE"
    max_n = 0
    for tok in existing_tokens:
        m = TOKEN_RE.fullmatch((tok or "").strip())
        if m and m.group(1) == cat:
            max_n = max(max_n, int(m.group(2)))
    return f"<*{cat}_{max_n + 1}*>"


# ----------------------------------------------------------------------
# guess_category — best-effort category for a value an API caller supplied
# directly with no category attached (see
# wiki/feature-updates/phase-1/plan-api-redaction-implementation.md, Section
# 3.4). DUPLICATED (same rationale as the module docstring) from a subset of
# dual_redaction_pipeline.py's structured regex patterns
# (dual_redaction_pipeline.py:980-996), hardcoded rather than pulled from
# pii_rules.yaml so this module still needs no config/pipeline dependency.
# Matched as a whole-string match, not a substring search, since the caller
# supplies the exact value to classify. Free text (names, diseases, arbitrary
# words) matches none of these and falls back to the generic "PII" category —
# real PERSON/CONDITION/ORG-level detection would need NER, deliberately out
# of scope here (heavy model, not worth loading into the always-on web app
# just to label a token prefix).
# ----------------------------------------------------------------------

_CATEGORY_PATTERNS = [
    ("EMAIL", re.compile(r'[\w.\-+]+@[\w.\-]+\.[a-zA-Z]{2,}')),
    ("MEDICARE", re.compile(r'\d{4}\s?\d{5}\s?\d{1}(?:\s?\d{1})?')),
    ("BSB_ACCOUNT", re.compile(r'\d{3}[-\s]\d{3}\s+\d{6,10}')),
    ("ABN", re.compile(r'\d{2}\s?\d{3}\s?\d{3}\s?\d{3}')),
    ("PHONE", re.compile(
        r'(?:\+?61\s?)?(?:0[2-9]\s?\d{4}\s?\d{4}|0?4\d{2}\s?\d{3}\s?\d{3}'
        r'|1[38]00\s?\d{3}\s?\d{3}|13\s?\d{2}\s?\d{2})')),
    ("TFN", re.compile(r'\d{3}[\s\-]?\d{3}[\s\-]?\d{3}')),
    ("ACN", re.compile(r'\d{3}\s?\d{3}\s?\d{3}')),
]


def guess_category(value: str) -> str:
    """Best-effort category for a bare API-supplied value — first pattern in
    _CATEGORY_PATTERNS that fully matches the (stripped) value wins; no match
    -> generic "PII" fallback."""
    v = value.strip()
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.fullmatch(v):
            return category
    return "PII"


# ----------------------------------------------------------------------
# apply_masking — copied from dual_redaction_pipeline.py:2061, minus the
# get_candidate_token() minting branch (manual adds always pass an
# already-minted token in global_entities, so that branch never fires).
# ----------------------------------------------------------------------

def apply_masking(text, global_entities: dict):
    if not text or not str(text).strip():
        return text, []

    masked = str(text)
    events = []

    for val_lower, tok in global_entities.items():
        if len(val_lower) < 3:
            continue
        pattern = rf'\b{re.escape(val_lower)}\b'
        match = re.search(pattern, masked, flags=re.IGNORECASE)
        if not match:
            continue
        actual_val = match.group(0)
        new_masked = re.sub(pattern, tok, masked, flags=re.IGNORECASE)
        if new_masked != masked:
            events.append({'value': actual_val, 'token': tok})
            masked = new_masked

    return masked, events


def _replace_with_spans(text: str, mapping: dict):
    """Substitute every occurrence of every mapping key (token -> original
    value) in `text` in a single left-to-right pass, returning the new text,
    the {token, value} events, and the (start, end) span of each inserted
    value within the new text (used to highlight just the restored value in
    unmask_docx_as_is, without touching surrounding formatting).

    Unlike a naive per-key `.replace()` loop, matches from every key are
    collected up front and resolved left-to-right so an earlier substitution
    can't shift the offsets of one recorded before it."""
    matches = []
    for cand, orig in mapping.items():
        start = 0
        while True:
            idx = text.find(cand, start)
            if idx == -1:
                break
            matches.append((idx, idx + len(cand), cand, orig))
            start = idx + len(cand)
    matches.sort(key=lambda m: m[0])

    filtered = []
    last_end = -1
    for m in matches:
        if m[0] >= last_end:
            filtered.append(m)
            last_end = m[1]

    new_text = ""
    spans = []
    events = []
    cursor = 0
    for start, end, cand, orig in filtered:
        new_text += text[cursor:start]
        span_start = len(new_text)
        new_text += orig
        spans.append((span_start, len(new_text)))
        events.append({'token': cand, 'value': orig})
        cursor = end
    new_text += text[cursor:]
    return new_text, events, spans


def _make_audit_event(ts, filename, ev, page_num=1, row=None, col=None) -> dict:
    return {
        'timestamp': ts, 'filename': filename,
        'page_num': page_num, 'excel_row': row, 'excel_col': col,
        'token_type': ev['token'], 'original_text': ev['value'],
    }


# ----------------------------------------------------------------------
# Word/PDF text-position helpers — copied from dual_redaction_pipeline.py
# (_pdf_page_words:3311, _group_words_into_lines:3352, _find_spans_in_line:3372,
# _image_words:2773, _load_redaction_font:2932)
# ----------------------------------------------------------------------

def _pdf_page_words(page, use_ocr_fallback: bool = True):
    native = [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words") if w[4].strip()]
    has_images = bool(page.get_images(full=True))
    # A page can be "hybrid": real native text (e.g. a page-number footer)
    # sitting alongside an embedded image (a logo, a scanned form) that the
    # native text layer can't see at all. Only skip OCR when there's no
    # embedded image to miss anything from -- otherwise everything inside
    # that image is silently unsearchable/unmaskable forever.
    if not use_ocr_fallback or (native and not has_images):
        return native, False

    import io
    import pytesseract
    import shutil
    tess_cmd = shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    pytesseract.pytesseract.tesseract_cmd = tess_cmd

    render_dpi = 300
    pix = page.get_pixmap(dpi=render_dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    tsv = pytesseract.image_to_data(img, config='--psm 6', output_type=pytesseract.Output.DICT)

    scale = 72.0 / render_dpi
    ocr_words = []
    for i in range(len(tsv['text'])):
        txt = (tsv['text'][i] or '').strip()
        try:
            conf = int(tsv['conf'][i])
        except (ValueError, TypeError):
            conf = -1
        if conf > 20 and txt:
            x0 = tsv['left'][i] * scale
            y0 = tsv['top'][i] * scale
            x1 = (tsv['left'][i] + tsv['width'][i]) * scale
            y1 = (tsv['top'][i] + tsv['height'][i]) * scale
            ocr_words.append((x0, y0, x1, y1, txt))

    if not native:
        return ocr_words, True

    # Hybrid page: the OCR pass re-read the WHOLE rendered page, so it just
    # re-detected the native text too (from the pixels), alongside whatever
    # was only visible inside the embedded image. Keep the exact native
    # words and only add OCR words that don't just duplicate them, so native
    # text isn't matched (and masked) twice.
    def _covered_by_native(ocr_word):
        cx, cy = (ocr_word[0] + ocr_word[2]) / 2, (ocr_word[1] + ocr_word[3]) / 2
        return any(nw[0] <= cx <= nw[2] and nw[1] <= cy <= nw[3] for nw in native)

    ocr_words = [w for w in ocr_words if not _covered_by_native(w)]
    return native + ocr_words, True


def _pdf_page_words_cached(path: Path, page_num: int, page, use_ocr_fallback: bool = True):
    """Cache-wrapping `_pdf_page_words()` by (file identity, page_num,
    use_ocr_fallback) so preview rendering and single-value masking — which
    both call this independently on the same on-disk file — reuse one OCR
    result instead of risking two Tesseract passes disagreeing on the same
    page. See the cache comment near IMAGE_EXTENSIONS."""
    st = path.stat()
    key = (str(path.resolve()), st.st_mtime_ns, st.st_size, page_num, use_ocr_fallback)
    hit = _PDF_WORDS_CACHE.get(key)
    if hit is not None:
        _PDF_WORDS_CACHE.move_to_end(key)
        return hit
    result = _pdf_page_words(page, use_ocr_fallback=use_ocr_fallback)
    _PDF_WORDS_CACHE[key] = result
    if len(_PDF_WORDS_CACHE) > _PDF_WORDS_CACHE_MAX:
        _PDF_WORDS_CACHE.popitem(last=False)
    return result


def _group_words_into_lines(words, y_tol: float = 4.0):
    lines = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        cy = (w[1] + w[3]) / 2
        placed = False
        for line in lines:
            if abs(line['cy'] - cy) <= y_tol:
                line['words'].append(w)
                line['cy'] = sum((ww[1] + ww[3]) / 2 for ww in line['words']) / len(line['words'])
                placed = True
                break
        if not placed:
            lines.append({'cy': cy, 'words': [w]})
    for line in lines:
        line['words'].sort(key=lambda w: w[0])
    return lines


def _find_spans_in_line(line_words, needles: list):
    texts = [w[4] for w in line_words]
    line_text = " ".join(texts)
    line_text_lower = line_text.lower()

    offsets = []
    pos = 0
    for t in texts:
        offsets.append(pos)
        pos += len(t) + 1

    covered = [False] * len(line_text)
    matches = []
    for needle_lower, replacement in needles:
        # Needle may come from a manually-typed value or a different OCR/extraction
        # pass than line_text, and can carry stray leading/trailing or doubled
        # whitespace that would otherwise defeat an exact substring match — see the
        # matching copy of this fix in dual_redaction_pipeline.py's _find_spans_in_line.
        needle_lower = re.sub(r'\s+', ' ', needle_lower).strip()
        if len(needle_lower) < 3:
            continue
        start = 0
        while True:
            idx = line_text_lower.find(needle_lower, start)
            if idx == -1:
                break
            end = idx + len(needle_lower)
            start = idx + 1
            if any(covered[idx:end]):
                continue

            first_wi = last_wi = None
            for wi, off in enumerate(offsets):
                w_end = off + len(texts[wi])
                if w_end > idx and off < end:
                    if first_wi is None:
                        first_wi = wi
                    last_wi = wi
            if first_wi is None:
                continue

            box_words = line_words[first_wi:last_wi + 1]
            x0 = min(w[0] for w in box_words)
            y0 = min(w[1] for w in box_words)
            x1 = max(w[2] for w in box_words)
            y1 = max(w[3] for w in box_words)
            matches.append(((x0, y0, x1, y1), line_text[idx:end], replacement))
            for k in range(idx, end):
                covered[k] = True
    return matches


def _max_half_width_for_line(x0: float, x1: float, line_words: list, pad: float) -> float:
    """How far a replacement-token box centered on the match spanning [x0,x1]
    may expand to each side before it reaches another word on the same line
    (kept `pad` clear of that neighbor's own edge). Used to keep a wide token
    like <*ORGANIZATION_3*> from overwriting/redacting part of an adjacent,
    unmatched word when it's drawn wider than the text it replaces. Never
    smaller than the match's own half-width, and unconstrained (inf) on a
    side with no neighboring word (start/end of line)."""
    left_limit = right_limit = None
    for w in line_words:
        wx0, wx1 = w[0], w[2]
        if wx1 <= x0 + 0.01:
            if left_limit is None or wx1 > left_limit:
                left_limit = wx1
        elif wx0 >= x1 - 0.01:
            if right_limit is None or wx0 < right_limit:
                right_limit = wx0
    cx = (x0 + x1) / 2
    left_room = (cx - left_limit - pad) if left_limit is not None else float('inf')
    right_room = (right_limit - pad - cx) if right_limit is not None else float('inf')
    return max(min(left_room, right_room), (x1 - x0) / 2)


def _shrink_to_fit(token: str, rect_width: float, base_fontsize: float, min_fontsize: float,
                    max_half_w: float, gap: float, measure_fn, step: float = 0.5):
    """Shrink `token`'s draw fontsize from base_fontsize down to min_fontsize
    (in `step` increments) until its box — max(rect_width, text_width + gap),
    split evenly either side of center — fits within max_half_w. Falls back
    to a hard clamp at min_fontsize if it still doesn't fit, so the caller's
    box can never exceed max_half_w (and therefore never reach a neighboring
    word) regardless of how long the token is."""
    fontsize = base_fontsize
    text_w = measure_fn(token, fontsize)
    half_w = max(rect_width, text_w + gap) / 2
    while half_w > max_half_w and fontsize > min_fontsize:
        fontsize = max(min_fontsize, fontsize - step)
        text_w = measure_fn(token, fontsize)
        half_w = max(rect_width, text_w + gap) / 2
    half_w = min(half_w, max_half_w)
    return fontsize, text_w, half_w


def _load_image_for_ocr(image_path: Path):
    """Cheap, deterministic part of `_image_words()`: open + EXIF-transpose +
    RGB-convert the full-res image, plus a downscaled copy to feed Tesseract
    when the image exceeds MAX_IMG_SIDE. Split out so a cache hit on OCR
    words can still get a fresh `(orig_img, inv)` without re-running OCR."""
    orig_img = Image.open(str(image_path))
    orig_img = ImageOps.exif_transpose(orig_img)
    if orig_img.mode != "RGB":
        orig_img = orig_img.convert("RGB")

    ocr_img = orig_img
    scale = 1.0
    if max(orig_img.size) > MAX_IMG_SIDE:
        scale = MAX_IMG_SIDE / max(orig_img.size)
        new_size = (max(1, int(orig_img.width * scale)), max(1, int(orig_img.height * scale)))
        ocr_img = orig_img.resize(new_size, Image.LANCZOS)
    return orig_img, ocr_img, scale


def _image_words(image_path: Path):
    import pytesseract
    import shutil
    tess_cmd = shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    pytesseract.pytesseract.tesseract_cmd = tess_cmd

    orig_img, ocr_img, scale = _load_image_for_ocr(image_path)

    tsv = pytesseract.image_to_data(ocr_img, config='--psm 6', output_type=pytesseract.Output.DICT)
    inv = 1.0 / scale
    words = []
    for i in range(len(tsv['text'])):
        txt = (tsv['text'][i] or '').strip()
        try:
            conf = int(tsv['conf'][i])
        except (ValueError, TypeError):
            conf = -1
        if conf > 20 and txt:
            x0 = tsv['left'][i] * inv
            y0 = tsv['top'][i] * inv
            x1 = (tsv['left'][i] + tsv['width'][i]) * inv
            y1 = (tsv['top'][i] + tsv['height'][i]) * inv
            words.append((x0, y0, x1, y1, txt))
    return words, orig_img, inv


def _image_words_cached(image_path: Path):
    """Cache-wrapping `_image_words()` by file identity so preview rendering
    and single-value masking reuse one OCR word list instead of risking two
    independent Tesseract passes disagreeing on the same image. The `Image`
    object itself is never cached — callers (masking) draw directly onto it,
    so it must always be freshly loaded; only the OCR text/positions are
    cacheable. See the cache comment near IMAGE_EXTENSIONS."""
    st = image_path.stat()
    key = (str(image_path.resolve()), st.st_mtime_ns, st.st_size)
    words = _IMAGE_WORDS_CACHE.get(key)
    if words is not None:
        _IMAGE_WORDS_CACHE.move_to_end(key)
        orig_img, _ocr_img, scale = _load_image_for_ocr(image_path)
        return words, orig_img, 1.0 / scale
    words, orig_img, inv = _image_words(image_path)
    _IMAGE_WORDS_CACHE[key] = words
    if len(_IMAGE_WORDS_CACHE) > _IMAGE_WORDS_CACHE_MAX:
        _IMAGE_WORDS_CACHE.popitem(last=False)
    return words, orig_img, inv


_REDACTION_FONT_CACHE = {}


def _load_redaction_font(size: int):
    if size in _REDACTION_FONT_CACHE:
        return _REDACTION_FONT_CACHE[size]
    try:
        font = ImageFont.truetype("arial.ttf", size)
    except Exception:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
    _REDACTION_FONT_CACHE[size] = font
    return font


# ----------------------------------------------------------------------
# docx paragraph helpers — copied from dual_redaction_pipeline.py
# (_iter_docx_block_items:3082, _rewrite_paragraph_text:3180)
# ----------------------------------------------------------------------

def _iter_docx_block_items(document):
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph as DocxParagraph
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield DocxParagraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield DocxTable(child, document)


def _rewrite_paragraph_text(paragraph, transform_fn):
    runs = paragraph.runs
    if not runs:
        return []
    original_text = "".join(r.text for r in runs)
    if not original_text.strip():
        return []
    new_text, events = transform_fn(original_text)
    if not events or new_text == original_text:
        return []
    runs[0].text = new_text
    for r in runs[1:]:
        r.text = ""
    return events


def _add_formatted_run(paragraph, template, text: str, highlight: bool):
    """Append a run to `paragraph` with `text`, copying `template`'s font
    formatting (bold/italic/underline/name/size/color) and optionally
    highlighting it yellow. Used by _rewrite_paragraph_text_highlighted to
    rebuild a paragraph as several runs instead of collapsing it into one, so
    only the restored value itself is highlighted."""
    run = paragraph.add_run(text)
    run.font.bold = template.font.bold
    run.font.italic = template.font.italic
    run.font.underline = template.font.underline
    run.font.name = template.font.name
    run.font.size = template.font.size
    try:
        if template.font.color is not None and template.font.color.type is not None:
            run.font.color.rgb = template.font.color.rgb
    except Exception:
        pass
    if highlight:
        from docx.enum.text import WD_COLOR_INDEX
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW
    return run


def _rewrite_paragraph_text_highlighted(paragraph, transform_fn, highlight: bool = True):
    """Like _rewrite_paragraph_text, but transform_fn returns (new_text,
    events, spans) — see _replace_with_spans() — and each span is rebuilt as
    its own run, leaving the rest of the paragraph's formatting untouched.
    `highlight` controls whether that restored-value run is marked yellow —
    used to distinguish full-document rehydration (highlighted, so a reviewer
    can see what was restored) from the reference table's single-row delete
    (not highlighted — that's just correcting the redaction, not a rehydrate).
    Only used for unmasking; masking keeps using the plain
    collapse-to-one-run version above."""
    runs = paragraph.runs
    if not runs:
        return []
    original_text = "".join(r.text for r in runs)
    if not original_text.strip():
        return []
    new_text, events, spans = transform_fn(original_text)
    if not events or new_text == original_text:
        return []

    template = runs[0]
    for r in runs:
        r.text = ""

    cursor = 0
    for span_start, span_end in spans:
        if span_start > cursor:
            _add_formatted_run(paragraph, template, new_text[cursor:span_start], False)
        _add_formatted_run(paragraph, template, new_text[span_start:span_end], highlight)
        cursor = span_end
    if cursor < len(new_text):
        _add_formatted_run(paragraph, template, new_text[cursor:], False)
    return events


def _add_formatted_run_pptx(paragraph, template, text: str, highlight: bool):
    """pptx equivalent of _add_formatted_run() above. python-pptx's Font
    object has no highlight_color property (unlike python-docx's
    WD_COLOR_INDEX) — PowerPoint runs don't expose a simple highlight API —
    so `highlight` is accepted for call-site symmetry with the docx path but
    has no visible effect here; the text substitution itself is identical."""
    run = paragraph.add_run()
    run.text = text
    run.font.bold = template.font.bold
    run.font.italic = template.font.italic
    run.font.underline = template.font.underline
    run.font.name = template.font.name
    run.font.size = template.font.size
    try:
        if template.font.color is not None and template.font.color.type is not None:
            run.font.color.rgb = template.font.color.rgb
    except Exception:
        pass
    return run


def _rewrite_pptx_paragraph_text_highlighted(paragraph, transform_fn, highlight: bool = True):
    """pptx equivalent of _rewrite_paragraph_text_highlighted() above — see
    _add_formatted_run_pptx() for why `highlight` has no visual effect here."""
    runs = paragraph.runs
    if not runs:
        return []
    original_text = "".join(r.text for r in runs)
    if not original_text.strip():
        return []
    new_text, events, spans = transform_fn(original_text)
    if not events or new_text == original_text:
        return []

    template = runs[0]
    for r in runs:
        r.text = ""

    cursor = 0
    for span_start, span_end in spans:
        if span_start > cursor:
            _add_formatted_run_pptx(paragraph, template, new_text[cursor:span_start], False)
        _add_formatted_run_pptx(paragraph, template, new_text[span_start:span_end], highlight)
        cursor = span_end
    if cursor < len(new_text):
        _add_formatted_run_pptx(paragraph, template, new_text[cursor:], False)
    return events


# ----------------------------------------------------------------------
# Word COM automation (.doc <-> .docx, and docx/doc -> PDF for preview) —
# doc<->docx conversion copied from dual_redaction_pipeline.py
# (convert_doc_to_docx:3025, convert_docx_to_doc:3147). Requires MS Word
# installed on the host, same as the existing pipeline's "as is" .doc support.
# ----------------------------------------------------------------------

def convert_doc_to_docx(doc_path: Path, output_docx_path: Path) -> bool:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.gencache.EnsureDispatch("Word.Application")
        word.Visible = False
        wdoc = word.Documents.Open(str(doc_path.resolve()), ReadOnly=True)
        try:
            wdoc.SaveAs2(str(output_docx_path.resolve()), FileFormat=12)  # wdFormatXMLDocument
        finally:
            wdoc.Close(False)
        return True
    except Exception as e:
        print(f"[ERROR] Word COM conversion (doc->docx) failed for {doc_path.name}: {e}")
        return False
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def convert_docx_to_doc(docx_path: Path, output_doc_path: Path) -> bool:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.gencache.EnsureDispatch("Word.Application")
        word.Visible = False
        wdoc = word.Documents.Open(str(docx_path.resolve()), ReadOnly=True)
        try:
            wdoc.SaveAs2(str(output_doc_path.resolve()), FileFormat=0)  # wdFormatDocument
        finally:
            wdoc.Close(False)
        return True
    except Exception as e:
        print(f"[ERROR] Word COM conversion (docx->doc) failed for {docx_path.name}: {e}")
        return False
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def convert_to_pdf_via_word(input_path: Path, output_pdf_path: Path) -> bool:
    """Export a .doc/.docx to PDF via MS Word COM automation — used for
    pixel-perfect preview rendering (reuses the same PyMuPDF page-to-image
    path as native PDF preview, since Word's own PDF export drives it)."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.gencache.EnsureDispatch("Word.Application")
        word.Visible = False
        wdoc = word.Documents.Open(str(input_path.resolve()), ReadOnly=True)
        try:
            wdoc.SaveAs2(str(output_pdf_path.resolve()), FileFormat=17)  # wdFormatPDF
        finally:
            wdoc.Close(False)
        return True
    except Exception as e:
        print(f"[ERROR] Word COM conversion to PDF failed for {input_path.name}: {e}")
        return False
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def convert_to_pdf_via_powerpoint(input_path: Path, output_pdf_path: Path) -> bool:
    """Export a .pptx to PDF via MS PowerPoint COM automation — used for
    pixel-perfect preview rendering, mirroring convert_to_pdf_via_word()
    above. Unlike Word, PowerPoint.Application.Visible cannot reliably be set
    to False in COM automation on every Office version — WithWindow=False on
    Presentations.Open() is the supported way to keep it out of the way
    without touching .Visible."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    powerpoint = None
    presentation = None
    try:
        powerpoint = win32com.client.gencache.EnsureDispatch("PowerPoint.Application")
        presentation = powerpoint.Presentations.Open(
            str(input_path.resolve()), WithWindow=False, ReadOnly=True
        )
        presentation.SaveAs(str(output_pdf_path.resolve()), FileFormat=32)  # ppSaveAsPDF
        return True
    except Exception as e:
        print(f"[ERROR] PowerPoint COM conversion to PDF failed for {input_path.name}: {e}")
        return False
    finally:
        if presentation is not None:
            try:
                presentation.Close()
            except Exception:
                pass
        if powerpoint is not None:
            try:
                powerpoint.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


# ----------------------------------------------------------------------
# Per-format MASK writers — copied from dual_redaction_pipeline.py
# (write_redacted_xlsx_as_is:3211, write_redacted_xls_as_is:3237,
# write_redacted_docx_as_is:3273, write_redacted_pdf_as_is:3427,
# write_redacted_image_as_is:2949), minus the unused secret_map/
# candidate_counters params (see apply_masking() above).
# ----------------------------------------------------------------------

def write_redacted_xlsx_as_is(input_path: Path, output_path: Path, global_entities: dict) -> list:
    import openpyxl
    wb = openpyxl.load_workbook(str(input_path))
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name

    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.strip():
                    masked, events = apply_masking(cell.value, global_entities)
                    if events:
                        cell.value = masked
                        for ev in events:
                            audit_records.append(_make_audit_event(
                                ts, filename, ev, page_num=ws.title, row=cell.row, col=cell.column))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return audit_records


def write_redacted_xls_as_is(input_path: Path, output_path: Path, global_entities: dict) -> list:
    import xlrd
    import xlwt

    rb = xlrd.open_workbook(str(input_path), formatting_info=False)
    wb = xlwt.Workbook()
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name

    for sheet_idx in range(rb.nsheets):
        rs = rb.sheet_by_index(sheet_idx)
        ws = wb.add_sheet(rs.name or f"Sheet{sheet_idx + 1}")
        for r in range(rs.nrows):
            for c in range(rs.ncols):
                val = rs.cell_value(r, c)
                if isinstance(val, str) and val.strip():
                    masked, events = apply_masking(val, global_entities)
                    if events:
                        for ev in events:
                            audit_records.append(_make_audit_event(
                                ts, filename, ev, page_num=rs.name, row=r, col=c))
                        ws.write(r, c, masked)
                        continue
                ws.write(r, c, val)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return audit_records


def write_redacted_docx_as_is(input_path: Path, output_path: Path, global_entities: dict) -> list:
    from docx import Document as DocxDocument
    from docx.text.paragraph import Paragraph as DocxParagraph

    doc = DocxDocument(str(input_path))
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name

    def transform(text):
        return apply_masking(text, global_entities)

    for section in doc.sections:
        for part in (section.header, section.footer):
            for para in part.paragraphs:
                for ev in _rewrite_paragraph_text(para, transform):
                    audit_records.append(_make_audit_event(ts, filename, ev))

    row_idx = 0
    for block in _iter_docx_block_items(doc):
        if isinstance(block, DocxParagraph):
            for ev in _rewrite_paragraph_text(block, transform):
                audit_records.append(_make_audit_event(ts, filename, ev, row=row_idx))
            row_idx += 1
        else:  # DocxTable
            for row in block.rows:
                for col_idx, cell in enumerate(row.cells):
                    for para in cell.paragraphs:
                        for ev in _rewrite_paragraph_text(para, transform):
                            audit_records.append(_make_audit_event(ts, filename, ev, row=row_idx, col=col_idx))
                row_idx += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return audit_records


def write_redacted_pptx_as_is(input_path: Path, output_path: Path, global_entities: dict) -> list:
    """Mirrors write_redacted_docx_as_is() above — python-pptx paragraphs/runs
    expose the same .runs / run.text shape as python-docx, so
    _rewrite_paragraph_text() works unchanged here."""
    from pptx import Presentation

    prs = Presentation(str(input_path))
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name

    def transform(text):
        return apply_masking(text, global_entities)

    for slide_idx, slide in enumerate(prs.slides):
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    for ev in _rewrite_paragraph_text(para, transform):
                        audit_records.append(_make_audit_event(ts, filename, ev, page_num=slide_idx + 1))
            if getattr(shape, "has_table", False):
                for row_idx, row in enumerate(shape.table.rows):
                    for col_idx, cell in enumerate(row.cells):
                        for para in cell.text_frame.paragraphs:
                            for ev in _rewrite_paragraph_text(para, transform):
                                audit_records.append(_make_audit_event(
                                    ts, filename, ev, page_num=slide_idx + 1, row=row_idx, col=col_idx))
        if slide.has_notes_slide:
            for para in slide.notes_slide.notes_text_frame.paragraphs:
                for ev in _rewrite_paragraph_text(para, transform):
                    audit_records.append(_make_audit_event(ts, filename, ev, page_num=slide_idx + 1))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))
    return audit_records


def write_redacted_email_as_is(input_path: Path, output_path: Path, global_entities: dict) -> list:
    """Mask an .eml/.msg in place (headers + text bodies), preserving the email
    format. Mirrors the other write_redacted_*_as_is() writers but delegates the
    format read/write to email_utils (stdlib email for .eml, Outlook COM for
    .msg)."""
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name
    audit_records = []

    def transform(text):
        masked, events = apply_masking(text, global_entities)
        for ev in events:
            audit_records.append(_make_audit_event(ts, filename, ev))
        return masked, events

    email_utils.rewrite_email(input_path, output_path, transform)
    return audit_records


def write_redacted_pdf_as_is(input_path: Path, output_path: Path, global_entities: dict) -> list:
    doc = fitz.open(str(input_path))
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name
    needles = sorted(global_entities.items(), key=lambda kv: -len(kv[0]))

    for page_num in range(len(doc)):
        page = doc[page_num]
        words, is_ocr = _pdf_page_words_cached(input_path, page_num, page, use_ocr_fallback=True)
        if not words:
            continue
        lines = _group_words_into_lines(words, y_tol=6.0 if is_ocr else 4.0)

        page_matches = []
        for line in lines:
            for m in _find_spans_in_line(line['words'], needles):
                page_matches.append((m, line['words']))
        if not page_matches:
            continue

        draws = []
        for (bbox, matched_text, token), line_words in page_matches:
            x0, y0, x1, y1 = bbox
            rect = fitz.Rect(x0, y0, x1, y1)
            base_fontsize = max(6, min(10, rect.height * 0.8))
            # MuPDF auto-shrinks annotation text to fit the rect using an internal
            # line-height reserve of ~2.3x fontsize, which a single-line word bbox
            # never has — so we white-out only, then draw the text ourselves at the
            # exact fontsize computed above (matches the working image-path approach).
            # The box is capped to the gap to the nearest neighboring word on the same
            # line (shrinking the font first) so a wide token never redact-annotates —
            # and thus deletes — part of an adjacent, unmatched word.
            max_half_w = _max_half_width_for_line(rect.x0, rect.x1, line_words, pad=1.5)
            fontsize, text_w, half_w = _shrink_to_fit(
                token, rect.width, base_fontsize, 4.0, max_half_w, gap=4,
                measure_fn=lambda t, fs: fitz.get_text_length(t, fontname="helv", fontsize=fs),
            )
            cx = (rect.x0 + rect.x1) / 2
            wide_rect = fitz.Rect(cx - half_w, rect.y0, cx + half_w, rect.y1)
            page.add_redact_annot(wide_rect, fill=(1, 1, 1))
            baseline_y = y0 + (rect.height + fontsize * 0.7) / 2
            draws.append((cx - text_w / 2, baseline_y, token, fontsize))
            audit_records.append(_make_audit_event(
                ts, filename, {'token': token, 'value': matched_text}, page_num=page_num + 1))
        page.apply_redactions()
        for origin_x, baseline_y, token, fontsize in draws:
            page.insert_text((origin_x, baseline_y), token, fontname="helv", fontsize=fontsize, color=(0, 0, 0))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()
    return audit_records


def write_redacted_image_as_is(input_path: Path, output_path: Path, global_entities: dict) -> list:
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name
    needles = sorted(global_entities.items(), key=lambda kv: -len(kv[0]))

    words, img, inv = _image_words_cached(input_path)
    draw = ImageDraw.Draw(img)

    if words:
        lines = _group_words_into_lines(words, y_tol=12.0 * inv)
        matches = []
        for line in lines:
            for m in _find_spans_in_line(line['words'], needles):
                matches.append((m, line['words']))

        def _measure(t, fs):
            f = _load_redaction_font(max(1, int(round(fs))))
            try:
                return draw.textlength(t, font=f)
            except Exception:
                return fs * len(t) * 0.6

        for (bbox, matched_text, token), line_words in matches:
            x0, y0, x1, y1 = bbox
            box_h = y1 - y0
            base_fontsize = max(10, min(28, int(box_h * 0.8)))
            # Cap the box to the gap to the nearest neighboring word on the same
            # line (shrinking the font first) so a wide token never overwrites
            # part of an adjacent, unmatched word's pixels.
            max_half_w = _max_half_width_for_line(x0, x1, line_words, pad=2.0)
            fontsize, text_w, half_w = _shrink_to_fit(
                token, x1 - x0, base_fontsize, 6, max_half_w, gap=8, measure_fn=_measure, step=1,
            )
            font = _load_redaction_font(max(1, int(round(fontsize))))
            box_w = half_w * 2
            cx = (x0 + x1) / 2
            bx0, bx1 = cx - box_w / 2, cx + box_w / 2
            draw.rectangle([bx0, y0, bx1, y1], fill=(255, 255, 255))
            tx = bx0 + max(0, (box_w - text_w) / 2)
            ty = y0 + max(0, (box_h - fontsize) / 2)
            draw.text((tx, ty), token, fill=(0, 0, 0), font=font)
            audit_records.append(_make_audit_event(
                ts, filename, {'token': token, 'value': matched_text}, page_num=1))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs = {'quality': 92} if output_path.suffix.lower() in ('.jpg', '.jpeg') else {}
    img.save(str(output_path), **save_kwargs)
    return audit_records


# ----------------------------------------------------------------------
# Per-format UNMASK (restore one value) — copied from
# dual_redaction_pipeline.py (unmask_xlsx_as_is:3523, unmask_xls_as_is:3540,
# unmask_docx_as_is:3560, unmask_pdf_as_is:3590)
# ----------------------------------------------------------------------

def unmask_xlsx_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict, highlight: bool = True):
    import openpyxl
    from openpyxl.styles import PatternFill
    HIGHLIGHT_FILL = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")

    wb = openpyxl.load_workbook(str(redacted_path))
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value:
                    new_val = cell.value
                    for cand, orig in mapping.items():
                        if cand in new_val:
                            new_val = new_val.replace(cand, orig)
                    if new_val != cell.value:
                        cell.value = new_val
                        if highlight:
                            cell.fill = HIGHLIGHT_FILL
    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(unmasked_path))


def unmask_xls_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict, highlight: bool = True):
    import xlrd
    import xlwt
    HIGHLIGHT_STYLE = xlwt.easyxf('pattern: pattern solid, fore_colour yellow;')

    rb = xlrd.open_workbook(str(redacted_path), formatting_info=False)
    wb = xlwt.Workbook()
    for sheet_idx in range(rb.nsheets):
        rs = rb.sheet_by_index(sheet_idx)
        ws = wb.add_sheet(rs.name or f"Sheet{sheet_idx + 1}")
        for r in range(rs.nrows):
            for c in range(rs.ncols):
                val = rs.cell_value(r, c)
                changed = False
                if isinstance(val, str) and val:
                    for cand, orig in mapping.items():
                        if cand in val:
                            val = val.replace(cand, orig)
                            changed = True
                ws.write(r, c, val, HIGHLIGHT_STYLE if (changed and highlight) else xlwt.Style.default_style)
    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(unmasked_path))


def unmask_docx_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict, highlight: bool = True):
    from docx import Document as DocxDocument
    doc = DocxDocument(str(redacted_path))

    def transform(text):
        return _replace_with_spans(text, mapping)

    for section in doc.sections:
        for part in (section.header, section.footer):
            for para in part.paragraphs:
                _rewrite_paragraph_text_highlighted(para, transform, highlight)

    from docx.text.paragraph import Paragraph as DocxParagraph
    for block in _iter_docx_block_items(doc):
        if isinstance(block, DocxParagraph):
            _rewrite_paragraph_text_highlighted(block, transform, highlight)
        else:
            for row in block.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        _rewrite_paragraph_text_highlighted(para, transform, highlight)

    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(unmasked_path))


def unmask_pptx_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict, highlight: bool = True):
    from pptx import Presentation
    prs = Presentation(str(redacted_path))

    def transform(text):
        return _replace_with_spans(text, mapping)

    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    _rewrite_pptx_paragraph_text_highlighted(para, transform, highlight)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        for para in cell.text_frame.paragraphs:
                            _rewrite_pptx_paragraph_text_highlighted(para, transform, highlight)
        if slide.has_notes_slide:
            for para in slide.notes_slide.notes_text_frame.paragraphs:
                _rewrite_pptx_paragraph_text_highlighted(para, transform, highlight)

    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(unmasked_path))


def unmask_email_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict, highlight: bool = True):
    """Restore original values in a redacted .eml/.msg (headers + text bodies).
    `highlight` is accepted for dispatcher symmetry but not applied — there's no
    cell/run fill to colour in email text the way there is in xlsx/docx."""
    def transform(text):
        new_text = text
        events = []
        for cand, orig in mapping.items():
            if cand in new_text:
                new_text = new_text.replace(cand, orig)
                events.append({'token': cand, 'value': orig})
        return new_text, events

    email_utils.rewrite_email(redacted_path, unmasked_path, transform)


def unmask_pdf_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict, highlight: bool = True):
    doc = fitz.open(str(redacted_path))
    needles = sorted(((tok.lower(), orig) for tok, orig in mapping.items()), key=lambda kv: -len(kv[0]))

    for page_num in range(len(doc)):
        page = doc[page_num]
        words, _ = _pdf_page_words(page, use_ocr_fallback=False)
        if not words:
            continue
        lines = _group_words_into_lines(words, y_tol=4.0)

        page_matches = []
        for line in lines:
            for m in _find_spans_in_line(line['words'], needles):
                page_matches.append((m, line['words']))
        if not page_matches:
            continue

        draws = []
        for (bbox, matched_text, orig_value), line_words in page_matches:
            x0, y0, x1, y1 = bbox
            rect = fitz.Rect(x0, y0, x1, y1)
            base_fontsize = max(6, min(10, rect.height * 0.8))
            # Same neighbor-gap cap as write_redacted_pdf_as_is — the restored
            # original value can be wider than the token it replaces, so it must
            # not redact-annotate into an adjacent, unmatched word either.
            max_half_w = _max_half_width_for_line(rect.x0, rect.x1, line_words, pad=1.5)
            fontsize, text_w, half_w = _shrink_to_fit(
                orig_value, rect.width, base_fontsize, 4.0, max_half_w, gap=4,
                measure_fn=lambda t, fs: fitz.get_text_length(t, fontname="helv", fontsize=fs),
            )
            cx = (rect.x0 + rect.x1) / 2
            wide_rect = fitz.Rect(cx - half_w, rect.y0, cx + half_w, rect.y1)
            fill = (1, 1, 0) if highlight else (1, 1, 1)  # yellow for rehydration, white for a plain restore
            page.add_redact_annot(wide_rect, fill=fill)
            baseline_y = y0 + (rect.height + fontsize * 0.7) / 2
            draws.append((cx - text_w / 2, baseline_y, orig_value, fontsize))
        page.apply_redactions()
        for origin_x, baseline_y, orig_value, fontsize in draws:
            page.insert_text((origin_x, baseline_y), orig_value, fontname="helv", fontsize=fontsize, color=(0, 0, 0))

    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(unmasked_path), garbage=4, deflate=True)
    doc.close()


def find_tokens_in_text(text: str) -> set:
    return {m.group(0) for m in TOKEN_RE.finditer(text or "")}


def find_tokens_in_file(path: Path) -> set:
    """Every token-looking string (e.g. "<*PERSON_1*>") actually present in an
    arbitrary document — used to scope rehydration to only the tokens that
    matter for *this* uploaded file (see
    wiki/feature-updates/phase-1/plan-rehyrdration-implementation.md,
    Section 3.0.2 step 1). Dispatches on extension; raises ValueError for
    unsupported formats (images have no searchable text layer)."""
    ext = path.suffix.lower()
    tokens = set()

    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for v in row:
                    if isinstance(v, str):
                        tokens |= find_tokens_in_text(v)
        wb.close()
        return tokens

    if ext == ".xls":
        import xlrd
        rb = xlrd.open_workbook(str(path), formatting_info=False)
        for si in range(rb.nsheets):
            rs = rb.sheet_by_index(si)
            for r in range(rs.nrows):
                for c in range(rs.ncols):
                    v = rs.cell_value(r, c)
                    if isinstance(v, str):
                        tokens |= find_tokens_in_text(v)
        return tokens

    if ext == ".docx":
        from docx import Document as DocxDocument
        from docx.text.paragraph import Paragraph as DocxParagraph
        doc = DocxDocument(str(path))
        for section in doc.sections:
            for part in (section.header, section.footer):
                for para in part.paragraphs:
                    tokens |= find_tokens_in_text(para.text)
        for block in _iter_docx_block_items(doc):
            if isinstance(block, DocxParagraph):
                tokens |= find_tokens_in_text(block.text)
            else:
                for row in block.rows:
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            tokens |= find_tokens_in_text(para.text)
        return tokens

    if ext == ".doc":
        tmp = path.with_name(f"{path.stem}__scan_tmp.docx")
        try:
            if not convert_doc_to_docx(path, tmp):
                raise RuntimeError("Word COM conversion (doc->docx) failed")
            return find_tokens_in_file(tmp)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    if ext == ".pdf":
        doc = fitz.open(str(path))
        for page in doc:
            tokens |= find_tokens_in_text(page.get_text())
        doc.close()
        return tokens

    if ext == ".pptx":
        from pptx import Presentation
        prs = Presentation(str(path))
        for slide in prs.slides:
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    tokens |= find_tokens_in_text(shape.text_frame.text)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        for cell in row.cells:
                            tokens |= find_tokens_in_text(cell.text_frame.text)
            if slide.has_notes_slide:
                tokens |= find_tokens_in_text(slide.notes_slide.notes_text_frame.text)
        return tokens

    if ext in EMAIL_EXTENSIONS:
        for chunk in email_utils.extract_email_text(path):
            tokens |= find_tokens_in_text(str(chunk))
        return tokens

    raise ValueError(f"Unsupported extension for token scan: {ext}")


def unmask_uploaded_file(upload_path: Path, output_path: Path, mapping: dict, highlight: bool = True) -> None:
    """Same per-format substitution as unmask_*_as_is above, but keyed off an
    arbitrary uploaded file's own extension rather than pii_files.output_format
    — the upload is a document derived elsewhere (e.g. an ITR draft), not the
    pipeline's own stored redacted output. `highlight=False` restores values
    without the yellow marking (finished summaries downloaded via
    /api/rehydrate/file)."""
    ext = upload_path.suffix.lower()
    if ext == ".xlsx":
        unmask_xlsx_as_is(upload_path, output_path, mapping, highlight)
    elif ext == ".xls":
        unmask_xls_as_is(upload_path, output_path, mapping, highlight)
    elif ext == ".docx":
        unmask_docx_as_is(upload_path, output_path, mapping, highlight)
    elif ext == ".doc":
        _unmask_doc(upload_path, output_path, mapping, highlight)
    elif ext == ".pptx":
        unmask_pptx_as_is(upload_path, output_path, mapping, highlight)
    elif ext in EMAIL_EXTENSIONS:
        unmask_email_as_is(upload_path, output_path, mapping, highlight)
    elif ext == ".pdf":
        unmask_pdf_as_is(upload_path, output_path, mapping, highlight)
    else:
        raise ValueError(f"Unsupported extension for rehydration: {ext}")


def rehydrate_uploaded_file(upload_path: Path, output_path: Path, token_map: dict, highlight: bool = True) -> dict:
    """Rehydrate an arbitrary uploaded document against a process's full,
    conflict-preserving token map (db.get_redaction_map_for_process()) —
    `token_map` is `{redacted_text: {source_text: [file_id, ...]}}`.

    Scans the upload for every token actually present, substitutes the
    unambiguous ones, and leaves anything conflicting or unknown exactly as
    literal text in the output (best-effort with a warning — see
    wiki/feature-updates/phase-1/plan-rehyrdration-implementation.md, Open
    Question NEW-1, option (c)). Returns
    `{matched, conflicts, total_known, zero_match}`; `zero_match=True` means
    none of this process's tokens were found in the upload at all (caller
    should treat this as a hard error per Open Question #5)."""
    found_tokens = find_tokens_in_file(upload_path)
    matched = found_tokens & token_map.keys()

    if not matched:
        return {"matched": 0, "conflicts": [], "total_known": len(token_map), "zero_match": True}

    safe = {}
    conflicts = []
    for tok in matched:
        candidates = token_map[tok]
        if len(candidates) == 1:
            safe[tok] = next(iter(candidates))
        else:
            conflicts.append({"token": tok, "candidates": candidates})

    unmask_uploaded_file(upload_path, output_path, safe, highlight)
    return {"matched": len(matched), "conflicts": conflicts, "total_known": len(token_map), "zero_match": False}


def _mask_doc(input_doc_path: Path, output_doc_path: Path, entities: dict) -> list:
    tmp_in = input_doc_path.with_name(f"{input_doc_path.stem}__conv_in.docx")
    tmp_out = input_doc_path.with_name(f"{input_doc_path.stem}__conv_out.docx")
    try:
        if not convert_doc_to_docx(input_doc_path, tmp_in):
            raise RuntimeError("Word COM conversion (doc->docx) failed")
        events = write_redacted_docx_as_is(tmp_in, tmp_out, entities)
        if events:
            if not convert_docx_to_doc(tmp_out, output_doc_path):
                raise RuntimeError("Word COM conversion (docx->doc) failed")
        return events
    finally:
        for tmp in (tmp_in, tmp_out):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


def _unmask_doc(input_doc_path: Path, output_doc_path: Path, mapping: dict, highlight: bool = True) -> None:
    tmp_in = input_doc_path.with_name(f"{input_doc_path.stem}__conv_in.docx")
    tmp_out = input_doc_path.with_name(f"{input_doc_path.stem}__conv_out.docx")
    try:
        if not convert_doc_to_docx(input_doc_path, tmp_in):
            raise RuntimeError("Word COM conversion (doc->docx) failed")
        unmask_docx_as_is(tmp_in, tmp_out, mapping, highlight)
        if not convert_docx_to_doc(tmp_out, output_doc_path):
            raise RuntimeError("Word COM conversion (docx->doc) failed")
    finally:
        for tmp in (tmp_in, tmp_out):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


# ----------------------------------------------------------------------
# Public dispatch API — this is what app.py calls.
# ----------------------------------------------------------------------

def mask_single_value(redacted_path: Path, value: str, token: str) -> list:
    """Add one {value: token} mask to an already-redacted output file, in
    place. Searches the CURRENT redacted output (not the original upload) —
    any value the pipeline didn't already catch is still plaintext there, so
    no Raw Excel / original-file retention is needed. Returns the audit
    events produced; an empty list means the value wasn't found anywhere in
    the document (nothing is written in that case)."""
    ext = redacted_path.suffix.lower()
    entities = {value.lower(): token}
    tmp = redacted_path.with_name(f"{redacted_path.stem}__tmp{redacted_path.suffix}")
    try:
        if ext == ".xlsx":
            events = write_redacted_xlsx_as_is(redacted_path, tmp, entities)
        elif ext == ".xls":
            events = write_redacted_xls_as_is(redacted_path, tmp, entities)
        elif ext == ".docx":
            events = write_redacted_docx_as_is(redacted_path, tmp, entities)
        elif ext == ".doc":
            events = _mask_doc(redacted_path, tmp, entities)
        elif ext == ".pptx":
            events = write_redacted_pptx_as_is(redacted_path, tmp, entities)
        elif ext in EMAIL_EXTENSIONS:
            events = write_redacted_email_as_is(redacted_path, tmp, entities)
        elif ext == ".pdf":
            events = write_redacted_pdf_as_is(redacted_path, tmp, entities)
        elif ext in IMAGE_EXTENSIONS:
            events = write_redacted_image_as_is(redacted_path, tmp, entities)
        else:
            raise ValueError(f"Unsupported extension for masking: {ext}")
        if events:
            tmp.replace(redacted_path)
        return events
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def unmask_single_value(redacted_path: Path, token: str, value: str) -> None:
    """Restore one {token: value} mask in an already-redacted output file, in
    place. Not valid for image "as is" outputs — pixel redaction is
    destructive, use remask_image_from_original() instead.

    Unlike unmask_uploaded_file() (the actual /rehydrate flow), this is the
    reference table's "delete this row" action — it's correcting an
    over-eager redaction, not restoring a document for handoff, so the
    restored value is NOT highlighted yellow (highlight=False on every
    dispatch below)."""
    ext = redacted_path.suffix.lower()
    mapping = {token: value}
    tmp = redacted_path.with_name(f"{redacted_path.stem}__tmp{redacted_path.suffix}")
    try:
        if ext == ".xlsx":
            unmask_xlsx_as_is(redacted_path, tmp, mapping, highlight=False)
        elif ext == ".xls":
            unmask_xls_as_is(redacted_path, tmp, mapping, highlight=False)
        elif ext == ".docx":
            unmask_docx_as_is(redacted_path, tmp, mapping, highlight=False)
        elif ext == ".doc":
            _unmask_doc(redacted_path, tmp, mapping, highlight=False)
        elif ext == ".pptx":
            unmask_pptx_as_is(redacted_path, tmp, mapping, highlight=False)
        elif ext in EMAIL_EXTENSIONS:
            unmask_email_as_is(redacted_path, tmp, mapping, highlight=False)
        elif ext == ".pdf":
            unmask_pdf_as_is(redacted_path, tmp, mapping, highlight=False)
        else:
            raise ValueError(f"Unsupported extension for unmasking: {ext}")
        tmp.replace(redacted_path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def remask_image_from_original(original_encrypted_path: Path, output_path: Path, entities: dict) -> list:
    """Edit/delete a masked value in an "as is" image. Pixel redaction is
    destructive (no token text layer to search-and-replace), so the only way
    to change what's masked is to decrypt the stored original
    (_store_original_encrypted() in dual_redaction_pipeline.py) and redraw
    every box for the file's remaining redaction rows from scratch.
    `entities` should be every {value_lower: token} pair that must still be
    masked after this edit/delete (i.e. every pii_redaction_map row for the
    file except the one being removed/changed)."""
    original_bytes = crypto_utils.decrypt_bytes(Path(original_encrypted_path).read_bytes())
    tmp_src = output_path.with_name(f"{output_path.stem}__orig_tmp{output_path.suffix}")
    tmp_out = output_path.with_name(f"{output_path.stem}__tmp{output_path.suffix}")
    try:
        tmp_src.write_bytes(original_bytes)
        if entities:
            events = write_redacted_image_as_is(tmp_src, tmp_out, entities)
            tmp_out.replace(output_path)
        else:
            events = []
            tmp_src.replace(output_path)
        return events
    finally:
        for tmp in (tmp_src, tmp_out):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


def _preview_xlsx(path: Path) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    sheets = []
    for ws in wb.worksheets:
        rows = [["" if v is None else str(v) for v in row] for row in ws.iter_rows(values_only=True)]
        sheets.append({"name": ws.title, "rows": rows})
    wb.close()
    return {"type": "table", "sheets": sheets}


def _preview_xls(path: Path) -> dict:
    import xlrd
    rb = xlrd.open_workbook(str(path), formatting_info=False)
    sheets = []
    for si in range(rb.nsheets):
        rs = rb.sheet_by_index(si)
        rows = [
            ["" if rs.cell_value(r, c) is None else str(rs.cell_value(r, c)) for c in range(rs.ncols)]
            for r in range(rs.nrows)
        ]
        sheets.append({"name": rs.name or f"Sheet{si + 1}", "rows": rows})
    return {"type": "table", "sheets": sheets}


PREVIEW_DPI = 150


def _render_pdf_pages(path: Path) -> tuple:
    """Returns (images, page_texts, page_words): one rendered PNG data URI,
    one extracted text string, and one list of word boxes per page, 1:1 and
    in page order. page_texts drives _token_pages(); page_words
    ({text,x0,y0,x1,y1} in the SAME pixel space as the rendered image, i.e.
    scaled from PDF points to PREVIEW_DPI) drives the frontend's marquee
    selection tool — it maps a drawn rectangle back to the underlying text
    without a second OCR round trip."""
    doc = fitz.open(str(path))
    images = []
    page_texts = []
    page_words = []
    scale = PREVIEW_DPI / 72.0
    for page_num, page in enumerate(doc):
        pix = page.get_pixmap(dpi=PREVIEW_DPI)
        b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
        images.append(f"data:image/png;base64,{b64}")
        page_texts.append(page.get_text())
        words, _ = _pdf_page_words_cached(path, page_num, page)
        page_words.append([
            {"text": w[4], "x0": w[0] * scale, "y0": w[1] * scale, "x1": w[2] * scale, "y1": w[3] * scale}
            for w in words
        ])
    doc.close()
    return images, page_texts, page_words


def _token_pages(page_texts: list, tokens) -> dict:
    """{token: [1-based page numbers where it appears]} for every token
    (redacted_text value, e.g. "<*PERSON_1*>") found in page_texts — drives
    the reference table's "Page" column / click-to-jump-to-page."""
    result = {}
    for token in tokens or []:
        if not token:
            continue
        pages = [i + 1 for i, text in enumerate(page_texts) if token in text]
        if pages:
            result[token] = pages
    return result


def _preview_image(path: Path, tokens=None) -> dict:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    preview = {"type": "image", "image": f"data:{mime};base64,{b64}"}
    try:
        words, _, _ = _image_words_cached(path)
        preview["words"] = [
            {"text": w[4], "x0": w[0], "y0": w[1], "x1": w[2], "y1": w[3]} for w in words
        ]
    except Exception as e:
        # Best-effort — the marquee tool falls back to its OCR-crop endpoint
        # when no pre-extracted words are available, so this isn't fatal.
        print(f"[WARN] Word-box OCR failed for {path.name}: {e}")
        preview["words"] = []
    if tokens:
        # A standalone image is always a single "page" — every token
        # redacted in it necessarily lives on that one page.
        preview["token_pages"] = {tok: [1] for tok in tokens}
    return preview


def _preview_via_word(path: Path, tokens=None) -> dict:
    """Pixel-perfect docx/doc preview: export to PDF via Word COM, then reuse
    the native PDF page-to-image renderer. Cached next to the source file
    (invalidated by mtime) since Word COM conversion is comparatively slow
    and preview auto-loads on every result-page view."""
    cache_path = path.with_name(f"{path.stem}__preview_cache.pdf")
    if not cache_path.exists() or cache_path.stat().st_mtime < path.stat().st_mtime:
        if not convert_to_pdf_via_word(path, cache_path):
            raise RuntimeError(f"Word COM conversion to PDF failed for {path.name}")
    images, page_texts, page_words = _render_pdf_pages(cache_path)
    preview = {"type": "pages", "images": images, "words": page_words}
    if tokens:
        preview["token_pages"] = _token_pages(page_texts, tokens)
    return preview


def _render_pptx_pages_fallback(path: Path, tokens=None) -> dict:
    """Best-effort pptx preview when PowerPoint COM can't open the file at
    all (see _preview_via_powerpoint) — some pptx files are valid enough for
    python-pptx (and LibreOffice/Google Slides) to read but get rejected by
    PowerPoint's own stricter native parser with a generic "PowerPoint could
    not open the file" error; this has been observed for files whose
    docProps/core.xml <dc:creator> shows a non-Microsoft generator (e.g.
    PptxGenJS-produced pptx).

    Draws each slide's text-frame/table shapes as labeled boxes at their real
    position/size (from python-pptx's own EMU geometry) using PIL. Not
    pixel-perfect — no real fonts/colors/images, and no per-word boxes for
    the marquee tool ("words" is empty per page) — but always works since it
    never touches PowerPoint, and is enough to visually confirm what text is
    on a slide, and where, for redaction review."""
    from pptx import Presentation

    prs = Presentation(str(path))
    scale = PREVIEW_DPI / 914400  # EMU -> pixels at PREVIEW_DPI (914400 EMU/inch)
    slide_w = max(1, int(prs.slide_width * scale))
    slide_h = max(1, int(prs.slide_height * scale))
    font = _load_redaction_font(16)

    def draw_boxed_text(draw, text, x0, y0, x1, y1, outline):
        draw.rectangle([x0, y0, x1, y1], outline=outline, width=1)
        words = text.split()
        lines, cur = [], ""
        max_w = max(10, x1 - x0 - 8)
        for w in words:
            trial = f"{cur} {w}".strip()
            if draw.textlength(trial, font=font) > max_w and cur:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        if cur:
            lines.append(cur)
        ty = y0 + 4
        line_h = font.size + 4
        for line in lines:
            if ty > y1 - 4:
                break
            draw.text((x0 + 4, ty), line, fill=(20, 20, 20), font=font)
            ty += line_h

    images = []
    page_texts = []
    for slide in prs.slides:
        img = Image.new("RGB", (slide_w, slide_h), "white")
        draw = ImageDraw.Draw(img)
        slide_text_parts = []

        for shape in slide.shapes:
            try:
                sx0, sy0 = int(shape.left * scale), int(shape.top * scale)
                sx1, sy1 = sx0 + int(shape.width * scale), sy0 + int(shape.height * scale)
            except TypeError:
                continue  # some placeholder/shape types have no position
            # Connector/line shapes (common in flow diagrams) can have a
            # negative width/height — e.g. an arrow drawn from bottom-right
            # to top-left — which PIL's rectangle() rejects (requires
            # x0<=x1, y0<=y1). Normalize instead of skipping the shape.
            x0, x1 = min(sx0, sx1), max(sx0, sx1)
            y0, y1 = min(sy0, sy1), max(sy0, sy1)

            if getattr(shape, "has_table", False):
                rows, cols = len(shape.table.rows), len(shape.table.columns)
                cell_w = (x1 - x0) / max(1, cols)
                cell_h = (y1 - y0) / max(1, rows)
                draw.rectangle([x0, y0, x1, y1], outline=(120, 120, 120), width=1)
                for r_idx, row in enumerate(shape.table.rows):
                    for c_idx, cell in enumerate(row.cells):
                        cx0, cy0 = x0 + c_idx * cell_w, y0 + r_idx * cell_h
                        cx1, cy1 = cx0 + cell_w, cy0 + cell_h
                        text = cell.text_frame.text
                        if text.strip():
                            draw_boxed_text(draw, text, cx0, cy0, cx1, cy1, (160, 160, 160))
                            slide_text_parts.append(text)
            elif getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text
                if text.strip():
                    draw_boxed_text(draw, text, x0, y0, x1, y1, (180, 180, 180))
                    slide_text_parts.append(text)
            else:
                draw.rectangle([x0, y0, x1, y1], outline=(220, 220, 220), width=1)

        if slide.has_notes_slide:
            slide_text_parts.append(slide.notes_slide.notes_text_frame.text)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        images.append(f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}")
        page_texts.append("\n".join(slide_text_parts))

    preview = {"type": "pages", "images": images, "words": [[] for _ in images], "degraded": True}
    if tokens:
        preview["token_pages"] = _token_pages(page_texts, tokens)
    return preview


def _preview_via_powerpoint(path: Path, tokens=None) -> dict:
    """Pixel-perfect pptx preview: export to PDF via PowerPoint COM, then
    reuse the native PDF page-to-image renderer. Same mtime-invalidated cache
    strategy as _preview_via_word(). Falls back to
    _render_pptx_pages_fallback() when PowerPoint COM can't open the file at
    all, instead of failing the preview outright."""
    cache_path = path.with_name(f"{path.stem}__preview_cache.pdf")
    if not cache_path.exists() or cache_path.stat().st_mtime < path.stat().st_mtime:
        if not convert_to_pdf_via_powerpoint(path, cache_path):
            print(f"[WARN] PowerPoint COM couldn't open {path.name} — falling back to a basic layout preview")
            return _render_pptx_pages_fallback(path, tokens)
    images, page_texts, page_words = _render_pdf_pages(cache_path)
    preview = {"type": "pages", "images": images, "words": page_words}
    if tokens:
        preview["token_pages"] = _token_pages(page_texts, tokens)
    return preview


def render_preview(redacted_path: Path, tokens=None) -> dict:
    """Render a redacted output file for inline browser preview. Dispatches
    purely on file extension — a flattened "excel" output and an "as is"
    .xlsx output are both just xlsx workbooks and render identically.

    `tokens` (optional list of redacted_text values, e.g. from
    db.get_redaction_rows()) drives the "Page" column in the reference
    table: for paginated formats (pdf, docx/doc via Word, pptx via
    PowerPoint, and single-page images) the returned dict gets a
    `token_pages` map. Flattened xlsx/xls output has no real page concept
    (rows aren't paginated), so it's omitted there — the caller should show
    a blank Page cell."""
    ext = redacted_path.suffix.lower()
    if ext == ".xlsx":
        return _preview_xlsx(redacted_path)
    if ext == ".xls":
        return _preview_xls(redacted_path)
    if ext in (".docx", ".doc"):
        return _preview_via_word(redacted_path, tokens)
    if ext == ".pptx":
        return _preview_via_powerpoint(redacted_path, tokens)
    if ext == ".pdf":
        images, page_texts, page_words = _render_pdf_pages(redacted_path)
        preview = {"type": "pages", "images": images, "words": page_words}
        if tokens:
            preview["token_pages"] = _token_pages(page_texts, tokens)
        return preview
    if ext in IMAGE_EXTENSIONS:
        return _preview_image(redacted_path, tokens)
    if ext in EMAIL_EXTENSIONS:
        return _preview_email(redacted_path)
    raise ValueError(f"No preview available for extension: {ext}")


def _preview_email(path: Path) -> dict:
    """Preview a redacted .eml/.msg as a one-column table (reuses the table
    renderer): labelled headers then the body rendered to readable lines (the
    HTML body is converted to text so it shows as lines, not one raw-HTML blob).
    No page concept, so no token_pages (same as xlsx/xls)."""
    rows = [[line] for line in email_utils.email_preview_lines(path)]
    return {"type": "table", "sheets": [{"name": "Email", "rows": rows}]}


def _ocr_crop(image: Image.Image, x0: float, y0: float, x1: float, y1: float) -> str:
    import pytesseract
    import shutil
    tess_cmd = shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    pytesseract.pytesseract.tesseract_cmd = tess_cmd

    x0i, y0i = max(0, int(x0)), max(0, int(y0))
    x1i, y1i = min(image.width, int(x1)), min(image.height, int(y1))
    if x1i <= x0i or y1i <= y0i:
        return ""
    crop = image.crop((x0i, y0i, x1i, y1i)).convert("RGB")
    # A little white margin around the glyphs measurably improves OCR
    # accuracy on tight marquee-drawn crops vs. feeding the raw crop.
    pad = 12
    padded = Image.new("RGB", (crop.width + pad * 2, crop.height + pad * 2), "white")
    padded.paste(crop, (pad, pad))
    return pytesseract.image_to_string(padded, config="--psm 7").strip()


def ocr_region(redacted_path: Path, page, x0: float, y0: float, x1: float, y1: float) -> str:
    """OCR just the box the user marquee-drew in the preview. Fallback for
    when the marquee selection tool finds no pre-extracted word (see
    render_preview's `words`) under the drawn rectangle — e.g. a logo, a
    scanned-page OCR miss, or a box drawn slightly off-target. `page` is the
    1-based page number for paginated formats (pdf/docx), None for a
    standalone image; (x0,y0,x1,y1) must be in the same pixel space as the
    corresponding preview image (PREVIEW_DPI-rendered pixels for pages,
    native pixels for a standalone image)."""
    ext = redacted_path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        img = Image.open(str(redacted_path))
        img = ImageOps.exif_transpose(img)
        return _ocr_crop(img.convert("RGB"), x0, y0, x1, y1)

    if ext == ".pdf":
        source_path = redacted_path
    elif ext in (".docx", ".doc"):
        source_path = redacted_path.with_name(f"{redacted_path.stem}__preview_cache.pdf")
        if not source_path.exists():
            raise RuntimeError("No cached preview PDF to crop from — reload the preview first")
    else:
        raise ValueError(f"OCR-region not supported for extension: {ext}")

    if not page:
        raise ValueError("page is required for paginated formats")

    import io
    doc = fitz.open(str(source_path))
    try:
        pg = doc[int(page) - 1]
        pix = pg.get_pixmap(dpi=PREVIEW_DPI)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        return _ocr_crop(img, x0, y0, x1, y1)
    finally:
        doc.close()
