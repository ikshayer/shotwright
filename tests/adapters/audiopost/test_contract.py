"""Entity, allowlist and preset assertions taken from AudioPostProfile CONTRACT.md."""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError


@pytest.fixture(scope="module")
def audio_post_entities():
    """Resolve contract-named entities at runtime so this file collects before they land."""
    entities = importlib.import_module("orchestrator.domain.entities")
    return entities.AudioPostProfile, entities.AudioStage


def _stored_profile(**updates):
    profile = {
        "name": "valid-profile",
        "semver": "1.2.3",
        "intent": "A narrow-band security intercom.",
        "voice_stages": [
            {"name": "voice", "filter": "volume", "params": {"gain_db": 0.0}},
        ],
        "noise": None,
        "hum": None,
        "encode": {"codec": "pcm_s16le", "sample_rate": 22050},
        "pinned_binaries": {"ffmpeg": "ffmpeg version test-build"},
    }
    profile.update(updates)
    return profile


def _with_stage(data, *, filter_name=None, params=None, stage_name=None):
    changed = copy.deepcopy(data)
    stage = changed["voice_stages"][0]
    if filter_name is not None:
        stage["filter"] = filter_name
    if params is not None:
        stage["params"] = params
    if stage_name is not None:
        stage["name"] = stage_name
    return changed


def test_audio_post_profile_and_stage_entities_accept_a_valid_stored_record(audio_post_entities) -> None:
    profile_type, stage_type = audio_post_entities
    profile = profile_type.model_validate(_stored_profile())

    assert profile.name == "valid-profile"
    assert profile.semver == "1.2.3"
    assert profile.voice_stages[0].filter == "volume"
    assert isinstance(profile.voice_stages[0], stage_type)


@pytest.mark.parametrize("name", ["Upper", "-leading", "has_underscore", "", "x" * 65])
def test_profile_name_must_match_the_contract_pattern(audio_post_entities, name) -> None:
    profile_type, _ = audio_post_entities
    with pytest.raises((ValidationError, ValueError)):
        profile_type.model_validate(_stored_profile(name=name))


@pytest.mark.parametrize("semver", ["1.2", "1.2.3-rc1", "v1.2.3", "1.2.x"])
def test_profile_semver_must_have_three_numeric_components(audio_post_entities, semver) -> None:
    profile_type, _ = audio_post_entities
    with pytest.raises((ValidationError, ValueError)):
        profile_type.model_validate(_stored_profile(semver=semver))


@pytest.mark.parametrize(
    "filter_name,params,valid",
    [
        ("highpass", {"f": 20}, True),
        ("highpass", {"f": 20000}, True),
        ("highpass", {"f": 19}, False),
        ("highpass", {"f": 20001}, False),
        ("highpass", {"f": 1000, "poles": 1}, True),
        ("highpass", {"f": 1000, "poles": 2}, True),
        ("highpass", {"f": 1000, "poles": 3}, False),
        ("volume", {"gain_db": -60.0}, True),
        ("volume", {"gain_db": 60.0}, True),
        ("volume", {"gain_db": -60.01}, False),
        ("volume", {"gain_db": 60.01}, False),
        ("asoftclip", {"type": "hard", "threshold": 1.0}, True),
        ("asoftclip", {"type": "hard", "threshold": 0.0}, False),
        ("asoftclip", {"type": "hard", "threshold": 1.01}, False),
        ("aecho", {"in_gain": 1.0, "out_gain": 1.0,
                    "delays_ms": [1], "decays": [1.0]}, True),
        ("aecho", {"in_gain": 1.0, "out_gain": 1.5,
                    "delays_ms": [1], "decays": [1.0]}, False),
        ("aecho", {"in_gain": 0.0, "out_gain": 1.0,
                    "delays_ms": [1], "decays": [1.0]}, False),
        ("aecho", {"in_gain": 1.0, "out_gain": 1.0,
                    "delays_ms": [0], "decays": [0.5]}, False),
        ("aecho", {"in_gain": 1.0, "out_gain": 1.0,
                    "delays_ms": [1000], "decays": [1.0]}, True),
        ("aecho", {"in_gain": 1.0, "out_gain": 1.0,
                    "delays_ms": [1001], "decays": [0.5]}, False),
        ("aecho", {"in_gain": 1.0, "out_gain": 1.0,
                    "delays_ms": [10], "decays": [0.0]}, False),
        ("aecho", {"in_gain": 1.0, "out_gain": 1.0,
                    "delays_ms": [10], "decays": [0.0001]}, True),
        ("acompressor", {"threshold": 0.000976563, "ratio": 1,
                         "attack": 0.01, "release": 0.01, "makeup": 1}, True),
        ("acompressor", {"threshold": 0.00097, "ratio": 1,
                         "attack": 0.01, "release": 0.01, "makeup": 1}, False),
        ("acompressor", {"threshold": 1, "ratio": 20,
                         "attack": 2000, "release": 9000, "makeup": 64}, True),
        ("aresample", {"sample_rate": 1000}, True),
        ("aresample", {"sample_rate": 192000}, True),
        ("aresample", {"sample_rate": 999}, False),
        ("aresample", {"sample_rate": 192001}, False),
    ],
)
def test_filter_parameter_range_edges(audio_post_entities, filter_name, params, valid) -> None:
    profile_type, _ = audio_post_entities
    data = _with_stage(_stored_profile(), filter_name=filter_name, params=params)

    if valid:
        profile = profile_type.model_validate(data)
        assert profile.voice_stages[0].filter == filter_name
    else:
        with pytest.raises((ValidationError, ValueError)):
            profile_type.model_validate(data)


@pytest.mark.parametrize("bad_number", [True, float("nan"), float("inf"), float("-inf")])
def test_stage_numeric_values_reject_booleans_and_nonfinite_numbers(audio_post_entities, bad_number) -> None:
    profile_type, _ = audio_post_entities
    data = _with_stage(_stored_profile(), filter_name="volume", params={"gain_db": bad_number})

    with pytest.raises((ValidationError, ValueError)):
        profile_type.model_validate(data)


@pytest.mark.parametrize(
    "filter_name,params",
    [
        ("aecho", {"in_gain": 0.8, "out_gain": 0.4,
                    "delays_ms": [28, 47], "decays": [0.3]}),
        ("aecho", {"in_gain": 0.8, "out_gain": 0.4,
                    "delays_ms": [], "decays": [0.3]}),
    ],
)
def test_aecho_requires_matching_nonempty_delay_and_decay_lists(audio_post_entities, filter_name, params) -> None:
    profile_type, _ = audio_post_entities
    with pytest.raises((ValidationError, ValueError)):
        profile_type.model_validate(_with_stage(_stored_profile(), filter_name=filter_name, params=params))


def test_mulaw_encoding_requires_eight_kilohertz(audio_post_entities) -> None:
    profile_type, _ = audio_post_entities
    with pytest.raises((ValidationError, ValueError)):
        profile_type.model_validate(_stored_profile(encode={"codec": "pcm_mulaw", "sample_rate": 16000}))


@pytest.mark.parametrize(
    "noise,valid",
    [
        ({"color": "white", "amplitude": 0.5}, True),
        ({"color": "pink", "amplitude": 0.0001}, True),
        ({"color": "blue", "amplitude": 0.1}, False),
        ({"color": "pink", "amplitude": 0.0}, False),
        ({"color": "pink", "amplitude": 0.5001}, False),
    ],
)
def test_noise_color_and_amplitude_allowlist(audio_post_entities, noise, valid) -> None:
    profile_type, _ = audio_post_entities
    if valid:
        assert profile_type.model_validate(_stored_profile(noise=noise)).noise.amplitude == noise["amplitude"]
    else:
        with pytest.raises((ValidationError, ValueError)):
            profile_type.model_validate(_stored_profile(noise=noise))


def test_creator_preset_file_matches_contract_section_six_and_is_a_create_body(audio_post_entities) -> None:
    profile_type, _ = audio_post_entities
    path = Path(__file__).resolve().parents[3] / "orchestrator" / "adapters" / "audiopost" / "presets" / "cctv-overdriven.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    assert set(body) == {"audio_post_profile"}
    profile = body["audio_post_profile"]
    assert "pinned_binaries" not in profile
    stages = profile["voice_stages"]

    assert profile["semver"] == "1.0.0"
    assert [stage["filter"] for stage in stages] == [
        "highpass", "volume", "asoftclip", "lowpass", "highpass",
        "aecho", "acompressor",
    ]
    assert stages[0]["params"]["f"] == 350
    assert stages[0]["params"].get("poles", 2) == 2
    assert stages[1]["params"]["gain_db"] == 30
    assert stages[2]["params"] == {"type": "hard", "threshold": 0.25}
    assert stages[3]["params"]["f"] == 2800
    assert stages[4]["params"]["f"] == 450
    assert stages[5]["params"] == {
        "in_gain": 0.8, "out_gain": 0.45, "delays_ms": [28, 47], "decays": [0.3, 0.2],
    }
    assert stages[6]["params"] == {
        "threshold": 0.03, "ratio": 12, "attack": 5, "release": 500, "makeup": 4,
    }
    assert profile["noise"] == {"color": "pink", "amplitude": 0.035}
    assert profile["hum"] == {
        "frequency_hz": 60, "amplitude": 0.02,
        "harmonics": [{"multiple": 3, "relative": pytest.approx(1 / 3)}],
    }
    assert profile["encode"] == {"codec": "pcm_mulaw", "sample_rate": 8000}
    profile_type.model_validate(profile | {"pinned_binaries": {"ffmpeg": "fixture identity"}})


