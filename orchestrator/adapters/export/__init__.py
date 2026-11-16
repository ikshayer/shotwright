"""Delivery render for FR-13's "exported final plays in ordinary players".

See CONTRACT.md. This adapter composites, scales and encodes; it never regenerates, and
it never appends a ShotOutput.
"""

from orchestrator.adapters.export.runner import (
    DeliveryRequest,
    DeliveryResult,
    ExportAdapter,
    build_delivery_command,
    build_delivery_filter,
)

__all__ = [
    "DeliveryRequest",
    "DeliveryResult",
    "ExportAdapter",
    "build_delivery_command",
    "build_delivery_filter",
]
