"""Cheap copies of images, for sending to the model.

Images dominate what a turn costs. A phone photo or a rendered plan page is stored at full
resolution because the BROWSER should show it sharp — but a model reading "58 in" off a
sketch does not need 1700×2200, and the difference is most of the bill:

    1700 × 2200  ->  20 tiles of 512 px
    1024 × 1325  ->   6 tiles

Same legibility for reading dimensions, roughly a third of the cost. Six of those per turn
was the difference between a session that costs a little and one that eats an allowance
while "not even doing anything".

So each image gets a downscaled JPEG copy, made once and cached next to the original. The
original is never touched: it is what the user sees, and what any future higher-fidelity
analysis would use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

_log = logging.getLogger(__name__)

#: The long edge of a copy sent to the model. Large enough for handwritten dimensions on a
#: phone photo, small enough that a page costs about six tiles instead of twenty.
MODEL_LONG_EDGE: Final = 1024

#: JPEG quality. 80 keeps pencil lines and thin dimension leaders readable; below ~70 they
#: start to smear, which is exactly the detail the model needs.
MODEL_QUALITY: Final = 80

#: Where copies live. A sibling directory, so the originals directory stays exactly what
#: the user uploaded.
COPIES_DIRECTORY: Final = "model_copies"


@dataclass
class ModelCopyMaker:
    """Produces (and caches) the copy of an image that is sent to the model."""

    long_edge: int = MODEL_LONG_EDGE
    quality: int = MODEL_QUALITY

    def copy_of(self, original: Path) -> Path:
        """A cheap copy of ``original``, or the original when one cannot be made.

        Never raises: a turn that cannot be made cheaper must still happen. Falling back to
        the original costs more tokens, which is strictly better than failing to answer.
        """
        try:
            return self._copy_of(original)
        except Exception as error:  # pragma: no cover - defensive
            _log.warning("could not downscale %s, sending the original: %s", original.name, error)
            return original

    def _copy_of(self, original: Path) -> Path:
        from PIL import Image

        destination = self._destination(original)
        if destination.exists() and destination.stat().st_mtime >= original.stat().st_mtime:
            return destination

        with Image.open(original) as image:
            width, height = image.size
            if max(width, height) <= self.long_edge and original.suffix.lower() in (
                ".jpg",
                ".jpeg",
            ):
                # Already small and already JPEG: re-encoding would only lose detail.
                return original

            scale = min(1.0, self.long_edge / max(width, height))
            target = (max(1, round(width * scale)), max(1, round(height * scale)))
            # Alpha and palettes cannot be written as JPEG, and a plan on a transparent
            # background must not come out black.
            prepared = image.convert("RGB") if image.mode != "RGB" else image
            resized = prepared.resize(target, Image.LANCZOS) if scale < 1.0 else prepared

            destination.parent.mkdir(parents=True, exist_ok=True)
            staged = destination.with_suffix(".partial")
            resized.save(staged, format="JPEG", quality=self.quality, optimize=True)
            staged.replace(destination)

        return destination

    def _destination(self, original: Path) -> Path:
        return original.parent / COPIES_DIRECTORY / f"{original.stem}.jpg"

    def forget(self, original: Path) -> None:
        """Drop the cached copy for a reference that was deleted."""
        try:
            self._destination(original).unlink(missing_ok=True)
        except OSError:  # pragma: no cover - a cache that will not clear is harmless
            pass


__all__ = ["COPIES_DIRECTORY", "MODEL_LONG_EDGE", "MODEL_QUALITY", "ModelCopyMaker"]
