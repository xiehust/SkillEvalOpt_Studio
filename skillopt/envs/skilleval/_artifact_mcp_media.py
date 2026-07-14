"""Stable-descriptor media validation for Artifact MCP renders."""
from __future__ import annotations

import io
import os
import stat

from PIL import Image as PillowImage
from PIL import UnidentifiedImageError

from .inspectors.base import InspectionError, bounded_diagnostic
from .inspectors._secure_files import open_scratch_file

DEFAULT_MAX_MEDIA_BYTES = 25 * 1024 * 1024
MAX_MEDIA_BYTES = 100 * 1024 * 1024


def read_png(
    source: int | str,
    scratch_dir: str,
    *,
    remaining_pixels: int,
    remaining_bytes: int,
) -> tuple[bytes, dict]:
    if isinstance(source, int) and not isinstance(source, bool):
        descriptor = os.dup(source)
    else:
        try:
            descriptor = open_scratch_file(scratch_dir, source)
        except (OSError, InspectionError) as exc:
            raise InspectionError(
                f"rendered media could not be opened: {bounded_diagnostic(exc)}"
            ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise InspectionError("rendered media must be a regular file")
        if info.st_size <= 0 or info.st_size > remaining_bytes:
            raise InspectionError("rendered media byte budget exceeded")
        with os.fdopen(os.dup(descriptor), "rb") as media:
            data = media.read(remaining_bytes + 1)
    finally:
        os.close(descriptor)
    if len(data) != info.st_size or len(data) > remaining_bytes:
        raise InspectionError("rendered media byte budget exceeded")
    try:
        with PillowImage.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise InspectionError("rendered media must be PNG")
            width, height = image.size
            image.verify()
    except InspectionError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise InspectionError(
            f"rendered media is not a valid PNG: {bounded_diagnostic(exc)}"
        ) from exc
    pixels = width * height
    if width <= 0 or height <= 0 or pixels > remaining_pixels:
        raise InspectionError("rendered media pixel budget exceeded")
    return data, {
        "mime": "image/png",
        "width": width,
        "height": height,
        "bytes": len(data),
        "pixels": pixels,
    }
