"""Live-schema resolution of nested dynamic-combo inputs.

Per `orchestrator/adapters/minimax_h3/SCHEMA_VALIDATION_CONTRACT.md`. The schema here is
REAL: `data/object_info_pinned_0_44_0.json` is captured verbatim from the pinned ComfyUI
0.44.0 server (see its README for provenance). Where the contract asks for a negative case
the real schema cannot express, a synthetic descriptor is used and labelled as such -- it
proves the resolver's rule, not the installed server's shape.

Written before the implementation existed. Every assertion says what was observed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.adapters.minimax_h3.core import H3Request, build_api_graph
from orchestrator.adapters.minimax_h3.native import _validate_live_schema
from orchestrator.adapters.minimax_h3.schema import resolve_node_inputs

DATA = Path(__file__).parent / "data" / "object_info_pinned_0_44_0.json"
MODELS = {
    "diffusion": "MiniMax-H3_Ref2VA-NVFP4.safetensors",
    "text_encoder": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "video_vae": "minimax_h3_video_vae_int8_convrot.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(DATA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def save_video_inputs(schema: dict[str, Any]) -> dict[str, Any]:
    return schema["SaveVideo"]["input"]


def _graph() -> dict[str, Any]:
    request = H3Request(
        shot_id="s1", shot_version=1, run_id="r1",
        video_path="sources/a.mp4", source_sha256="a" * 64, source_output_id="o1",
        prompt="a quiet room tone",
    )
    return build_api_graph(request, models=MODELS, seed=7)


# --- contract test 1: the regression. The adapter's real graph must validate. ------------

def test_adapter_graph_validates_against_the_real_pinned_schema(schema):
    """The failing case from the first GPU run. Must fail against the unfixed validator."""
    maximum = _validate_live_schema(_graph(), schema)
    assert isinstance(maximum, int) and maximum > 0, (
        f"validation returned a non-positive RandomNoise maximum: {maximum!r}"
    )


def test_save_video_node_resolves_every_input_it_declares(save_video_inputs):
    node_inputs = _graph()["12"]["inputs"]
    resolved = resolve_node_inputs(save_video_inputs, node_inputs)
    unresolved = sorted(set(node_inputs) - set(resolved))
    assert not unresolved, (
        f"SaveVideo inputs the resolver could not place: {unresolved}; "
        f"resolved={sorted(resolved)}"
    )


# --- contract test 2: depth. Recursion, not a two-level special case. --------------------

def test_dotted_inputs_resolve_two_and_three_levels_deep(save_video_inputs):
    resolved = resolve_node_inputs(
        save_video_inputs,
        {"format": "mp4", "format.codec": "h264", "format.codec.encoding": "re-encode",
         "format.codec.encoding.crf": 20.0},
    )
    assert "format.codec" in resolved, "format.codec did not resolve one level down"
    assert "format.codec.encoding" in resolved, "format.codec.encoding did not resolve two levels down"
    assert "format.codec.encoding.crf" in resolved, (
        f"crf did not resolve three levels down; resolved={sorted(resolved)}"
    )
    crf = resolved["format.codec.encoding.crf"]
    assert crf[0] == "FLOAT", f"crf descriptor type was {crf[0]!r}, expected FLOAT"
    assert crf[1]["min"] == 0.0 and crf[1]["max"] == 51.0, (
        f"crf bounds were {crf[1].get('min')}..{crf[1].get('max')}, expected 0.0..51.0"
    )


def test_numeric_bounds_still_apply_to_a_nested_input(schema):
    """A nested FLOAT is not exempt from range checking just because it was reached by expansion."""
    graph = _graph()
    graph["12"]["inputs"]["format.codec.encoding"] = "re-encode"
    graph["12"]["inputs"]["format.codec.encoding.crf"] = 99.0  # max is 51.0
    with pytest.raises(Exception) as excinfo:
        _validate_live_schema(graph, schema)
    message = str(excinfo.value)
    # Must be the BOUNDS refusal, not "lacks inputs" -- the unfixed validator rejected
    # every dotted name, which would satisfy a looser assertion for the wrong reason.
    assert "maximum" in message and "51.0" in message, (
        f"nested crf=99.0 was not rejected on its declared 0.0..51.0 bounds; "
        f"raised {excinfo.value!r}"
    )


# --- contract test 3: value-direction, in BOTH directions. ------------------------------

def test_encoding_resolves_under_h264(save_video_inputs):
    resolved = resolve_node_inputs(
        save_video_inputs, {"format": "mp4", "format.codec": "h264", "format.codec.encoding": "auto"})
    assert "format.codec.encoding" in resolved, (
        "encoding is offered by the h264 codec option but did not resolve"
    )


def test_encoding_is_refused_under_a_codec_that_does_not_offer_it(save_video_inputs):
    """`auto` carries no sub-inputs. NOTE: av1 DOES offer encoding -- only `auto` is bare."""
    codec = save_video_inputs["required"]["format"][1]["options"]
    mp4 = next(o for o in codec if o["key"] == "mp4")
    auto = next(o for o in mp4["inputs"]["required"]["codec"][1]["options"] if o["key"] == "auto")
    assert not (auto["inputs"].get("required") or auto["inputs"].get("optional")), (
        f"premise broken: the `auto` codec option declares inputs {auto['inputs']!r}"
    )
    resolved = resolve_node_inputs(
        save_video_inputs, {"format": "mp4", "format.codec": "auto", "format.codec.encoding": "auto"})
    assert "format.codec.encoding" not in resolved, (
        "encoding resolved under codec=auto, which offers no sub-inputs -- the resolver "
        "flattened all options instead of following the selected one"
    )


def test_validator_refuses_a_nested_input_the_selection_does_not_offer(schema):
    graph = _graph()
    graph["12"]["inputs"]["format.codec"] = "auto"
    with pytest.raises(Exception) as excinfo:
        _validate_live_schema(graph, schema)
    assert "format.codec.encoding" in str(excinfo.value), (
        f"validator accepted format.codec.encoding under codec=auto; raised {excinfo.value!r}"
    )


# --- contract test 4: wrong selection at the top level. ---------------------------------
# All five real `format` options offer `codec`, so the real schema cannot express this.
# Synthetic descriptor, labelled: it proves the rule, not the installed shape.

SYNTHETIC_TOP = {
    "required": {
        "format": ["COMFY_DYNAMICCOMBO_V3", {"options": [
            {"key": "bare", "inputs": {"required": {}}},
            {"key": "rich", "inputs": {"required": {"codec": ["COMBO", {"options": ["h264"]}]}}},
        ]}],
    }
}


def test_sub_input_refused_when_the_chosen_top_option_does_not_offer_it():
    resolved = resolve_node_inputs(SYNTHETIC_TOP, {"format": "bare", "format.codec": "h264"})
    assert "format.codec" not in resolved, (
        "format.codec resolved although the chosen option `bare` declares no inputs "
        "(synthetic schema; proves the selection rule)"
    )


def test_sub_input_resolves_when_the_chosen_top_option_does_offer_it():
    resolved = resolve_node_inputs(SYNTHETIC_TOP, {"format": "rich", "format.codec": "h264"})
    assert "format.codec" in resolved, (
        "format.codec failed to resolve although the chosen option `rich` declares it "
        "(synthetic schema; proves the selection rule)"
    )


def test_unknown_selection_value_resolves_nothing_beneath_it():
    resolved = resolve_node_inputs(SYNTHETIC_TOP, {"format": "nosuch", "format.codec": "h264"})
    assert "format.codec" not in resolved, (
        "format.codec resolved under an option key that does not exist in the schema"
    )


# --- contract test 5: COMFY_AUTOGROW_V3. ------------------------------------------------
# NOTE: not on the adapter's critical path -- its graph uses the Easy plugin nodes, not
# MiniMaxH3ReferenceToVideo. Covered because the contract requires it.

@pytest.fixture(scope="module")
def r2v_inputs(schema: dict[str, Any]) -> dict[str, Any]:
    return schema["MiniMaxH3ReferenceToVideo"]["input"]


def test_autogrow_index_within_max_resolves(r2v_inputs):
    template = r2v_inputs["optional"]["ref_videos"][1]["template"]
    assert template["prefix"] == "ref_video_" and template["max"] == 3, (
        f"premise broken: autogrow template is {template.get('prefix')!r} max={template.get('max')!r}"
    )
    resolved = resolve_node_inputs(r2v_inputs, {"ref_videos.ref_video_0": ["2", 0]})
    assert "ref_videos.ref_video_0" in resolved, (
        f"ref_video_0 did not resolve against an autogrow max of {template['max']}"
    )
    assert resolved["ref_videos.ref_video_0"][0] == "IMAGE", (
        f"autogrow descriptor was {resolved['ref_videos.ref_video_0'][0]!r}, expected IMAGE"
    )


def test_autogrow_index_at_or_beyond_max_is_refused(r2v_inputs):
    resolved = resolve_node_inputs(r2v_inputs, {"ref_videos.ref_video_3": ["2", 0]})
    assert "ref_videos.ref_video_3" not in resolved, (
        "ref_video_3 resolved although autogrow max is 3, so valid indices are 0..2"
    )


def test_autogrow_name_not_matching_prefix_is_refused(r2v_inputs):
    resolved = resolve_node_inputs(r2v_inputs, {"ref_videos.not_a_ref": ["2", 0]})
    assert "ref_videos.not_a_ref" not in resolved, (
        "a child name not matching the autogrow prefix `ref_video_` resolved anyway"
    )


# --- an explicit flat declaration of a dotted name is honoured. -------------------------
# The real server nests, but a schema MAY declare a dotted name at top level, and the
# existing protocol fixtures (`test_native_process.py::schema_for`) do exactly that. The
# resolver must stay a strict superset of the flat membership test it replaced.

FLAT_DOTTED = {"required": {"format": ["STRING", {}], "format.codec": ["STRING", {}]}}


def test_flat_declaration_of_a_dotted_name_is_honoured():
    resolved = resolve_node_inputs(FLAT_DOTTED, {"format": "mp4", "format.codec": "h264"})
    assert resolved.get("format.codec") == ["STRING", {}], (
        "a dotted name declared explicitly at top level did not resolve to its own "
        f"descriptor; resolved={resolved!r}"
    )


def test_flat_declaration_does_not_rescue_an_undeclared_dotted_name():
    resolved = resolve_node_inputs(FLAT_DOTTED, {"format": "mp4", "format.other": "x"})
    assert "format.other" not in resolved, (
        "an undeclared dotted name resolved even though neither a flat declaration nor an "
        "option tree offers it"
    )


# --- contract test 6: nothing was weakened. ---------------------------------------------

def test_invented_flat_port_is_still_refused(schema):
    graph = _graph()
    graph["12"]["inputs"]["totally_invented"] = "x"
    with pytest.raises(Exception) as excinfo:
        _validate_live_schema(graph, schema)
    assert "totally_invented" in str(excinfo.value), (
        f"an invented flat port was accepted; raised {excinfo.value!r}"
    )


def test_invented_dotted_port_under_a_valid_parent_is_still_refused(schema):
    graph = _graph()
    graph["12"]["inputs"]["format.invented"] = "x"
    with pytest.raises(Exception) as excinfo:
        _validate_live_schema(graph, schema)
    assert "format.invented" in str(excinfo.value), (
        f"an invented sub-input under a real dynamic combo was accepted; raised {excinfo.value!r}"
    )


def test_dynamic_combo_value_outside_its_option_keys_is_refused(schema):
    """Names the bad value, so a cascading `format.codec` error cannot satisfy this by accident.

    The pre-fix validator never checked dynamic-combo values at all: descriptor[0] is the
    string "COMFY_DYNAMICCOMBO_V3" rather than a list of choices, so its membership test
    was skipped entirely. Resolving options makes this checkable, so it must be checked.
    """
    graph = _graph()
    graph["12"]["inputs"]["format"] = "not_a_container"
    with pytest.raises(Exception) as excinfo:
        _validate_live_schema(graph, schema)
    assert "not_a_container" in str(excinfo.value), (
        f"the unavailable `format` value itself was not named in the refusal; "
        f"raised {excinfo.value!r}"
    )


def test_unknown_node_class_is_still_refused(schema):
    graph = _graph()
    graph["12"]["class_type"] = "NoSuchNodeClass"
    with pytest.raises(Exception) as excinfo:
        _validate_live_schema(graph, schema)
    assert "NoSuchNodeClass" in str(excinfo.value), (
        f"an unknown node class was accepted; raised {excinfo.value!r}"
    )


# --- contract test 7: the hidden top-level `codec` input stays valid. -------------------

def test_hidden_optional_top_level_codec_is_accepted(save_video_inputs):
    assert "codec" in (save_video_inputs.get("optional") or {}), (
        "premise broken: the pinned schema no longer declares a top-level optional `codec`"
    )
    resolved = resolve_node_inputs(save_video_inputs, {"format": "mp4", "codec": "auto"})
    assert "codec" in resolved, (
        "the real top-level optional `codec` input failed to resolve"
    )
