"""Independent tests authored solely from the preimplementation contract."""

import json
import math

import pytest

from orchestrator.adapters.minimax_h3.core import H3Request, build_api_graph


def request(**changes):
    data = {
        "shot_id": "s1",
        "shot_version": 1,
        "run_id": "r1",
        "video_path": "generated/master.mp4",
        "source_sha256": "a" * 64,
        "source_output_id": "o1",
        "prompt": "  quiet\nroom \u96e8  ",
    }
    data.update(changes)
    return H3Request(**data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("shot_id", ""),
        ("run_id", "../bad"),
        ("source_output_id", "a b"),
        ("shot_version", True),
        ("shot_version", 1.0),
        ("shot_version", "1"),
        ("degrade_seed", True),
        ("degrade_seed", -1),
        ("fps", 24.0),
        ("steps", "20"),
        ("width", True),
        ("experimental", 1),
        ("start_seconds", math.nan),
        ("start_seconds", math.inf),
        ("start_seconds", -0.1),
        ("duration_seconds", math.nan),
        ("video_path", "../escape.mp4"),
        ("video_path", "C:/x.mp4"),
        ("video_path", "C:x.mp4"),
        ("video_path", "//server/share/x.mp4"),
        ("video_path", "x\x00.mp4"),
        ("source_sha256", "A" * 64),
        ("source_sha256", "a" * 63),
        ("prompt", " \n\t"),
        ("prompt", "x\x00y"),
    ],
)
def test_request_rejects_unsafe_or_coerced_fields(field, value):
    with pytest.raises(ValueError):
        request(**{field: value})


def test_exact_unicode_prompt_and_utf8_byte_limit():
    text = " \r\n\u96e8\U0001f440 \t"
    assert request(prompt=text).prompt == text
    assert request(prompt="a" * 65536).prompt == "a" * 65536
    with pytest.raises(ValueError):
        request(prompt="\u96e8" * 21846)
    with pytest.raises(ValueError):
        request(unexpected=True)


@pytest.mark.parametrize(
    "changes",
    [{"duration_seconds": 4}, {"width": 512}, {"height": 320}, {"fps": 25}, {"steps": 21}],
)
def test_off_baseline_requires_explicit_flag(changes):
    with pytest.raises(ValueError, match="experimental"):
        request(**changes)
    assert request(**changes, experimental=True).experimental


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_seconds": 0.1},
        {"duration_seconds": 30.1},
        {"duration_seconds": 1.25},
        {"width": 33},
        {"height": 16416},
        {"fps": 121},
        {"steps": 10001},
        {"duration_seconds": 0.2, "fps": 24},
        {"duration_seconds": 0.2, "fps": 20},
    ],
)
def test_experimental_keeps_schema_and_exact_frame_grid(changes):
    with pytest.raises(ValueError):
        request(**changes, experimental=True)


def test_valid_exact_frame_grid_and_explicit_baseline_flag():
    assert request(duration_seconds=0.5, fps=10, experimental=True).duration_seconds == 0.5
    assert request(experimental=True).experimental is True
    assert request().experimental is False


MODELS = {
    "diffusion": "ref.safetensors",
    "text_encoder": "encoder.safetensors",
    "video_vae": "video.safetensors",
    "audio_vae": "audio.safetensors",
}


def graph():
    return build_api_graph(request(), models=MODELS, seed=123)


def node(g, name):
    found = [(k, n) for k, n in g.items() if n["class_type"] == name]
    assert len(found) == 1
    return found[0]


def test_graph_is_fresh_pure_exact_recipe_and_prompt():
    g = graph()
    assert set(g) == {"1", "2", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "42"}
    serialized = json.dumps(g)
    assert request().prompt in [v for n in g.values() for v in n["inputs"].values()]
    assert g["2"]["inputs"]["media"] == ["42", 0]
    assert g["2"]["inputs"]["h3_bundle"] == ["1", 0]
    assert g["1"]["inputs"]["fl2va_model"] == "None"
    for basename in MODELS.values():
        assert basename in serialized
    assert not any(
        s in serialized.lower() for s in ("lora", "cloud", "provider", "gain", "normalize")
    )
    assert node(g, "RandomNoise")[1]["inputs"]["noise_seed"] == 123
    scheduler = node(g, "BasicScheduler")[1]["inputs"]
    assert (
        scheduler["steps"] == 20
        and scheduler["scheduler"] == "simple"
        and scheduler["denoise"] == 1
    )
    assert node(g, "KSamplerSelect")[1]["inputs"]["sampler_name"] == "res_multistep"
    sampler = node(g, "SamplerCustomAdvanced")[0]
    for name in ("VAEDecode", "VAEDecodeAudio"):
        assert node(g, name)[1]["inputs"]["samples"] == [sampler, 0]
    assert "model_proxy.mp4" in json.dumps(json.loads(g["42"]["inputs"]["media_state"]))
    g["2"]["inputs"]["media"][0] = "bad"
    assert graph()["2"]["inputs"]["media"] == ["42", 0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("frame_count", True),
        ("fps", "24"),
        ("audio_sample_rate", 0),
        ("audio_channels", -1),
        ("audio_samples_per_channel", 1.0),
        ("av_path", "../candidate.mp4"),
        ("wav_path", "D:/audio.wav"),
        ("report_sha256", "z" * 64),
        ("experimental", "false"),
    ],
)
def test_result_strict_positive_metadata_and_contained_links(field, value):
    from orchestrator.adapters.minimax_h3.core import H3Result

    data = {
        "run_id": "r1",
        "av_path": "generated/candidate.mp4",
        "av_sha256": "a" * 64,
        "wav_path": "generated/audio.wav",
        "wav_sha256": "b" * 64,
        "report_path": "generated/report.json",
        "report_sha256": "c" * 64,
        "frame_count": 192,
        "fps": 24,
        "audio_sample_rate": 48000,
        "audio_channels": 2,
        "audio_samples_per_channel": 384000,
        "seed": 1,
        "experimental": False,
    }
    data[field] = value
    with pytest.raises(ValueError):
        H3Result(**data)


def test_graph_model_mapping_and_unsafe_media_names_rejected():
    for bad in ("../proxy.mp4", "C:/proxy.mp4", "https://example.com/x.mp4"):
        with pytest.raises(ValueError):
            build_api_graph(request(), models=MODELS, seed=1, input_filename=bad)
    with pytest.raises(ValueError):
        build_api_graph(request(), models={"diffusion": "only.safetensors"}, seed=1)


# The exact option list the pinned MiniMaxH3Easy node publishes for `aspect_ratio`,
# copied from this machine's /object_info. The adapter must only ever emit one of these.
OFFERED_ASPECT_RATIOS = ["1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9"]


def _aspect_of(**changes):
    g = build_api_graph(request(**changes), models=MODELS, seed=1)
    return node(g, "MiniMaxH3Easy")[1]["inputs"]["aspect_ratio"]


def test_aspect_ratio_is_always_one_the_node_offers():
    """Stage C died here: `352:288` is not a selectable option, so the run was refused.

    The old derivation emitted a literal `WIDTH:HEIGHT` string whenever the dimensions
    were not exactly 4:3, and such a string can never be in a fixed option list -- not
    even for a square frame, where it would emit `512:512` rather than `1:1`. Only
    exactly-4:3 input worked, which is why the 384x288 baseline passed and nothing caught
    it until a 352x288 CCTV shot was actually requested.
    """
    cases = [
        {},                                                        # 384x288 baseline, 4:3
        {"width": 352, "height": 288, "experimental": True},       # CIF: 11:9, displays 4:3
        {"width": 512, "height": 512, "experimental": True},       # square
        {"width": 1920, "height": 1088, "experimental": True},     # wide
        {"width": 1088, "height": 1920, "experimental": True},     # tall
        {"width": 640, "height": 480, "experimental": True},       # 4:3 in square pixels
    ]
    for changes in cases:
        value = _aspect_of(**changes)
        assert value in OFFERED_ASPECT_RATIOS, (
            f"{changes or 'baseline'} produced aspect_ratio={value!r}, "
            f"which the installed node does not offer"
        )


def test_cif_maps_to_its_display_aspect_not_its_storage_ratio():
    """352x288 must select 4:3.

    The PRD's aspect-ratio rule is explicit that CIF is 11:9 in square pixels and that the
    *display* aspect ratio is what must read 4:3 -- which is how the Phase 1 clip is
    already encoded (SAR 12:11, DAR 4:3). Selecting 3:2 or 1:1 here would be a legal
    option and still the wrong picture.
    """
    assert _aspect_of(width=352, height=288, experimental=True) == "4:3"


def test_portrait_and_landscape_are_not_confused():
    """Nearest-ratio selection must not collapse orientation: 9:16 is not 16:9."""
    assert _aspect_of(width=1920, height=1088, experimental=True) == "16:9"
    assert _aspect_of(width=1088, height=1920, experimental=True) == "9:16"


def test_the_baseline_aspect_ratio_is_unchanged():
    """384x288 emitted 4:3 before this change; this pins that nothing shifted."""
    assert _aspect_of() == "4:3"
