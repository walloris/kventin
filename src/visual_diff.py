"""
Visual diff: сравнение скриншотов до и после действия.
Определяет процент изменения и зону изменения.
"""
import base64
import logging
from typing import Optional, Tuple, Dict

LOG = logging.getLogger("VisualDiff")


def compute_screenshot_diff(
    before_b64: Optional[str],
    after_b64: Optional[str],
) -> Dict:
    """
    Сравнить два скриншота (base64 PNG).
    Возвращает:
    {
        "changed": bool,
        "change_percent": float (0-100),
        "diff_zone": "none"|"small"|"medium"|"large"|"full",
        "detail": str,
    }
    """
    if not before_b64 or not after_b64:
        return {"changed": True, "change_percent": 100.0, "diff_zone": "full", "detail": "нет данных для сравнения"}

    if before_b64 == after_b64:
        return {"changed": False, "change_percent": 0.0, "diff_zone": "none", "detail": "идентичные скриншоты"}

    try:
        from io import BytesIO
        from PIL import Image
        import numpy as np

        img1 = Image.open(BytesIO(base64.b64decode(before_b64))).convert("RGB")
        img2 = Image.open(BytesIO(base64.b64decode(after_b64))).convert("RGB")

        # Привести к одинаковому размеру
        if img1.size != img2.size:
            img2 = img2.resize(img1.size, Image.LANCZOS)

        arr1 = np.array(img1, dtype=float)
        arr2 = np.array(img2, dtype=float)

        # Попиксельная разница
        diff = np.abs(arr1 - arr2)
        pixel_diff = diff.mean(axis=2)  # средняя по RGB

        # Порог: пиксель считается изменённым, если разница > 30
        threshold = 30
        changed_pixels = (pixel_diff > threshold).sum()
        total_pixels = pixel_diff.size
        change_pct = (changed_pixels / total_pixels) * 100

        if change_pct < 0.5:
            zone = "none"
            detail = "практически без изменений"
        elif change_pct < 5:
            zone = "small"
            detail = f"мелкое изменение ({change_pct:.1f}%): тултип, dropdown, курсор"
        elif change_pct < 30:
            zone = "medium"
            detail = f"среднее изменение ({change_pct:.1f}%): модалка, секция страницы"
        elif change_pct < 70:
            zone = "large"
            detail = f"крупное изменение ({change_pct:.1f}%): навигация, перезагрузка"
        else:
            zone = "full"
            detail = f"полная смена экрана ({change_pct:.1f}%)"

        return {
            "changed": change_pct >= 0.5,
            "change_percent": round(change_pct, 1),
            "diff_zone": zone,
            "detail": detail,
        }
    except ImportError:
        LOG.debug("visual_diff: numpy/Pillow не установлены, сравнение по хешу")
        import hashlib
        h1 = hashlib.md5(before_b64[:5000].encode()).hexdigest()
        h2 = hashlib.md5(after_b64[:5000].encode()).hexdigest()
        changed = h1 != h2
        return {
            "changed": changed,
            "change_percent": 50.0 if changed else 0.0,
            "diff_zone": "medium" if changed else "none",
            "detail": "хеш-сравнение: изменилось" if changed else "хеш-сравнение: не изменилось",
        }
    except Exception as e:
        LOG.debug("visual_diff error: %s", e)
        return {"changed": True, "change_percent": -1, "diff_zone": "unknown", "detail": str(e)}
