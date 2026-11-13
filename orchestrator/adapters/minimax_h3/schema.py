"""Resolution of ComfyUI nested sub-inputs for live `/object_info` schema checks.

ComfyUI publishes a node's inputs as a flat `required`/`optional` mapping, but a
`COMFY_DYNAMICCOMBO_V3` input carries further inputs *inside the option that was
selected*, and those may nest again. The pinned `SaveVideo` nests three levels:

    format (auto|mp4|mkv|webm|mov)
      -> codec (auto|h264|av1|prores, per format option)
           -> encoding (auto|re-encode, offered by h264 and av1 but not auto)
                -> crf (FLOAT 0..51, offered by re-encode)

A graph addresses those with dotted names (``format.codec.encoding``). They are therefore
never present in the flat mapping, which is why a flat membership test rejects a correct
graph -- the defect the first GPU run found.

**Resolution is value-directed, and that is the point.** ``format.codec`` exists only
because the graph selected ``format = "mp4"``; ``format.codec.encoding`` only because it
then selected ``format.codec = "h264"``. Collecting the sub-inputs of *every* option into
one allow-list would accept ``format.codec.encoding`` under ``codec = "auto"``, which the
server does not offer -- trading an early, clear refusal for a late failure after a model
load. Names that do not resolve are simply omitted, so the caller still refuses them.

`spikes/22_minimax_h3_workflows/local_h3.py` has its own one-level version of this. It is
deliberately **not** shared from: it cannot express `format.codec.encoding`, and its
SHA-256 is recorded in every run's provenance receipt, so editing it would change that
receipt. This module is the superset and the only copy the orchestrator uses.
"""
from __future__ import annotations

from typing import Any

DYNAMIC_COMBO = "COMFY_DYNAMICCOMBO_V3"
AUTOGROW = "COMFY_AUTOGROW_V3"


def _flatten(inputs_schema: Any) -> dict[str, Any]:
    """Merge a schema node's ``required`` and ``optional`` sections into one mapping."""
    flat: dict[str, Any] = {}
    if not isinstance(inputs_schema, dict):
        return flat
    for section in ("required", "optional"):
        part = inputs_schema.get(section)
        if isinstance(part, dict):
            flat.update(part)
    return flat


def _meta(descriptor: Any) -> dict[str, Any]:
    if isinstance(descriptor, list) and len(descriptor) > 1 and isinstance(descriptor[1], dict):
        return descriptor[1]
    return {}


def dynamic_combo_option_keys(descriptor: Any) -> list[Any] | None:
    """Selectable keys of a dynamic combo, or None when it declares no option list."""
    options = _meta(descriptor).get("options")
    if not isinstance(options, list):
        return None
    return [option.get("key") for option in options if isinstance(option, dict)]


def _selected_option_inputs(descriptor: Any, selected: Any) -> dict[str, Any] | None:
    """Inputs offered by the option the graph selected, or None if that option is absent."""
    options = _meta(descriptor).get("options")
    if not isinstance(options, list):
        return None
    for option in options:
        if isinstance(option, dict) and option.get("key") == selected:
            return _flatten(option.get("inputs"))
    return None


def _autogrow_descriptor(descriptor: Any, child: str) -> Any:
    """Descriptor for one autogrow instance, honouring its prefix and min/max bounds."""
    template = _meta(descriptor).get("template")
    if not isinstance(template, dict):
        return None
    prefix = template.get("prefix")
    if not isinstance(prefix, str) or not prefix or not child.startswith(prefix):
        return None
    index_text = child[len(prefix):]
    if not index_text.isdigit():
        return None
    high = template.get("max")
    if not isinstance(high, int) or isinstance(high, bool):
        return None
    low = template.get("min", 0)
    if not isinstance(low, int) or isinstance(low, bool):
        low = 0
    if not low <= int(index_text) < high:
        return None
    offered = _flatten(template.get("input"))
    if len(offered) != 1:
        # The template declares no single instance input. Refuse rather than guess which
        # one a dotted name meant; a wrong guess would validate against the wrong type.
        return None
    return next(iter(offered.values()))


def resolve_node_inputs(inputs_schema: Any, node_inputs: Any) -> dict[str, Any]:
    """Map each graph input name to its schema descriptor, following the graph's selections.

    Flat names resolve directly. A dotted name resolves only when every level above it was
    selected to a value whose option actually offers the next component. Unresolvable
    names are omitted from the result, never guessed at, so the caller refuses them.
    """
    resolved: dict[str, Any] = {}
    if not isinstance(node_inputs, dict):
        return resolved
    flat = _flatten(inputs_schema)
    for name in node_inputs:
        if not isinstance(name, str) or not name:
            continue
        if name in flat:
            # An exact declaration wins, dotted or not. A schema that publishes
            # `format.codec` as a top-level key has declared it, and resolution must not
            # second-guess that; walking the option tree instead would reject a schema
            # that is explicit. This keeps the resolver a strict superset of the flat
            # membership test it replaces.
            resolved[name] = flat[name]
            continue
        parts = name.split(".")
        descriptor = flat.get(parts[0])
        if descriptor is None:
            continue
        path = parts[0]
        for child in parts[1:]:
            if not isinstance(descriptor, list) or not descriptor:
                descriptor = None
                break
            kind = descriptor[0]
            if kind == DYNAMIC_COMBO:
                offered = _selected_option_inputs(descriptor, node_inputs.get(path))
                descriptor = offered.get(child) if isinstance(offered, dict) else None
            elif kind == AUTOGROW:
                descriptor = _autogrow_descriptor(descriptor, child)
            else:
                descriptor = None
            if descriptor is None:
                break
            path = f"{path}.{child}"
        if descriptor is not None:
            resolved[name] = descriptor
    return resolved
