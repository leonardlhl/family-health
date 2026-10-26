"""Bounded PDF inspection and irreversible, pixel-only anonymization.

Never copy PDF objects from an incoming document into an archived document.
Only rendered RGB pixels enter a fresh PDF, after every page is reviewed.
"""
from contextlib import contextmanager
import io
import math
import threading

from PIL import Image, ImageDraw

from .errors import ValidationError

MAX_PDF_BYTES = 50_000_000
MAX_PDF_PAGES = 80
MAX_RENDER_PIXELS = 320_000_000
PDF_DPI = 180
PDF_EDGE = 3000
PDF_JPEG_QUALITY = 90
PDF_LOCK = threading.RLock()  # PyMuPDF operations must not run concurrently.


def _library():
    try:
        import pymupdf
    except ImportError:
        raise ValidationError("PDF 功能需要 PyMuPDF；請使用程式 requirements.txt 安裝依賴後再試。") from None
    # Parser diagnostics may contain embedded source strings; do not log them.
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    return pymupdf


def _geometry(page):
    width, height = float(page.rect.width), float(page.rect.height)
    if not all(math.isfinite(n) and 0 < n <= 14400 for n in (width, height)):
        raise ValidationError("PDF 頁面尺寸不支援，請另存為標準紙張尺寸。")
    scale = min(PDF_DPI / 72, PDF_EDGE / max(width, height))
    return width, height, scale, max(1, math.ceil(width * scale)), max(1, math.ceil(height * scale))


@contextmanager
def document(raw):
    if not isinstance(raw, bytes) or not raw.startswith(b"%PDF-") or not raw or len(raw) > MAX_PDF_BYTES:
        raise ValidationError("PDF 格式錯誤或超過 50 MB。")
    with PDF_LOCK:
        library = _library()
        doc = None
        try:
            doc = library.open(stream=raw, filetype="pdf")
            if doc.needs_pass or doc.is_encrypted:
                raise ValidationError("請先解除 PDF 密碼，再匯入處理。")
            if not 1 <= doc.page_count <= MAX_PDF_PAGES:
                raise ValidationError("PDF 須有 1 至 80 頁，較長報告請先分檔。")
            budget = 0
            for page in doc:
                *_, width, height = _geometry(page)
                budget += width * height
                if budget > MAX_RENDER_PIXELS:
                    raise ValidationError("PDF 總像素超過處理限制，請先分檔。")
            yield doc
        except ValidationError:
            raise
        except Exception:
            raise ValidationError("PDF 無法讀取或已損壞，請重新匯出原文件。") from None
        finally:
            if doc is not None:
                doc.close()


def info(raw):
    with document(raw) as doc:
        sizes = []
        for page in doc:
            _, _, _, width, height = _geometry(page)
            sizes.append({"width": width, "height": height})
        return {"pages": doc.page_count, "page_sizes": sizes, "render_dpi": PDF_DPI, "max_edge": PDF_EDGE}


def _pixels(page):
    library = _library()
    _, _, scale, _, _ = _geometry(page)
    pixmap = page.get_pixmap(matrix=library.Matrix(scale, scale), colorspace=library.csRGB, alpha=False, annots=True)
    if pixmap.width * pixmap.height > PDF_EDGE * PDF_EDGE:
        raise ValidationError("PDF 頁面超過像素限制。")
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def page_png(raw, page_number):
    with document(raw) as doc:
        if type(page_number) is not int or not 0 <= page_number < doc.page_count:
            raise ValidationError("PDF 頁碼不存在。")
        pixels = _pixels(doc[page_number])
        out = io.BytesIO()
        pixels.save(out, "PNG", optimize=True)
        return out.getvalue()


def _masks(pages, reviewed_pages, count):
    if not isinstance(reviewed_pages, list) or any(type(n) is not int for n in reviewed_pages) or len(reviewed_pages) != count or set(reviewed_pages) != set(range(count)):
        raise ValidationError("請逐頁檢查 PDF；所有頁面都須完成私隱核對。")
    if not isinstance(pages, list) or len(pages) > count:
        raise ValidationError("PDF 遮蓋清單格式錯誤。")
    masks = {}
    for item in pages:
        if not isinstance(item, dict) or type(item.get("page")) is not int or not 0 <= item["page"] < count or item["page"] in masks:
            raise ValidationError("PDF 遮蓋頁碼格式錯誤或重複。")
        boxes = item.get("boxes", [])
        if not isinstance(boxes, list) or len(boxes) > 200:
            raise ValidationError("每頁最多 200 個遮蓋區域。")
        masks[item["page"]] = []
        for box in boxes:
            if not isinstance(box, dict) or set(box) != {"x", "y", "w", "h"}:
                raise ValidationError("遮蓋區域須包含 x、y、w、h。")
            values = [box[key] for key in ("x", "y", "w", "h")]
            if any(type(n) not in {int, float} or not math.isfinite(n) for n in values):
                raise ValidationError("遮蓋座標必須為有效數字。")
            x, y, width, height = values
            if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 and 0 < height <= 1 and x + width <= 1.00000001 and y + height <= 1.00000001):
                raise ValidationError("遮蓋座標須為頁面內的 0 至 1 比例。")
            masks[item["page"]].append(values)
    return masks


def sanitize(raw, pages, reviewed_pages):
    with document(raw) as source:
        masks = _masks(pages, reviewed_pages, source.page_count)
        library = _library()
        with library.open() as target:
            total_encoded = 0
            for index, page in enumerate(source):
                pixels = _pixels(page)
                painter = ImageDraw.Draw(pixels)
                for x, y, width, height in masks.get(index, []):
                    # Outward rounding avoids leaving identifiable edge pixels.
                    left, top = math.floor(x * pixels.width), math.floor(y * pixels.height)
                    right, bottom = math.ceil((x + width) * pixels.width), math.ceil((y + height) * pixels.height)
                    painter.rectangle((left, top, right - 1, bottom - 1), fill="black")
                encoded = io.BytesIO()
                pixels.save(encoded, "JPEG", quality=PDF_JPEG_QUALITY, subsampling=0, optimize=True)
                jpeg = encoded.getvalue()
                total_encoded += len(jpeg)
                if total_encoded > MAX_PDF_BYTES:
                    raise ValidationError("處理後 PDF 超過 50 MB，請先分檔。")
                output_page = target.new_page(width=page.rect.width, height=page.rect.height)
                output_page.insert_image(output_page.rect, stream=jpeg)
            target.set_metadata({})
            result = target.tobytes(garbage=4, deflate=True, clean=True, no_new_id=True)
        if len(result) > MAX_PDF_BYTES:
            raise ValidationError("處理後 PDF 超過 50 MB，請先分檔。")
        return result


def clean_restored(raw):
    """Rebuild trusted reviewed backup pixels without restoring PDF objects."""
    count = info(raw)["pages"]
    return sanitize(raw, [], list(range(count)))
