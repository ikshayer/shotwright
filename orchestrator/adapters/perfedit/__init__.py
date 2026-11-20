"""PerformanceEdit: pause widening, breaths, gain contour and a false start on a committed take (FR-32)."""

from .detect import PauseDetection, PerformanceEditRequestError
from .edit import ALGORITHM_VERSION, PerformanceEditSpec
from .runner import PerformanceEditAdapter, PerformanceEditRequest, PerformanceEditResult

__all__ = [
    "ALGORITHM_VERSION", "PauseDetection", "PerformanceEditAdapter", "PerformanceEditRequest",
    "PerformanceEditRequestError", "PerformanceEditResult", "PerformanceEditSpec",
]
