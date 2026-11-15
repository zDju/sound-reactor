from __future__ import annotations

from pathlib import Path

import pytest

from sound_reactor import profile as profiles
from sound_reactor.profile import Key, ProfileError, Wait

EXAMPLE = Path(__file__).parent.parent / "examples" / "wow-fishing.toml"


def test_example_profile():
    p = profiles.load(EXAMPLE)
    assert p.source == "app:World of Warcraft" and p.target == "World of Warcraft"
    assert p.sounds == {"bite": {"min_level_db": -40.0}}
    assert p.on_start == [Key("9")]
    assert p.on_sound[0].sound == "bite"
    assert p.on_sound[0].actions == [Wait(0.25), Key("f"), Wait(1.0), Key("9")]
    assert p.on_silence == profiles.SilenceRule(30.0, [Key("9")])
    assert p.stop == profiles.StopRules(60.0, 5, 100)
    assert (p.ignore_after_action, p.start_delay) == (1.5, 5.0)
    assert p.sound_names() == ["bite"] and p.key_names() == ["9", "f"]


def test_round_trip(tmp_path):
    p = profiles.load(EXAMPLE)
    p.sounds["other.sound"] = {"threshold": 0.7}
    p.on_sound.append(profiles.SoundRule("door", [Key("space")]))
    path = profiles.save(p, tmp_path / "x" / "p.toml")
    assert profiles.load(path) == p
    assert profiles.loads(profiles.dumps(p)) == p
    assert profiles.loads(profiles.dumps(profiles.Profile())) == profiles.Profile()


def test_spec_shape_with_integers_loads():
    p = profiles.loads(
        'source = "app:x"\ntarget = "y"\n[on_silence]\nseconds = 30\nactions = [{ wait = 1 }]\n'
        "[stop]\nmax_minutes = 60\n"
    )
    assert p.on_silence == profiles.SilenceRule(30.0, [Wait(1.0)])
    assert p.stop.max_minutes == 60.0 and p.stop.max_cycles is None
    assert p.on_silence is not None and isinstance(p.on_silence.seconds, float)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("sorce = 'x'", "unknown setting"),
        ("[on_start]\nactions = [{ key = 1 }]", "must be a string"),
        ("[on_start]\nactions = [{ key = 'f', wait = 1 }]", "step 1"),
        ("[on_start]\nactions = [{ key = 'a b' }]", "invalid key"),
        ("[on_start]\nactions = [{ wait = -1 }]", ">= 0"),
        ("[[on_sound]]\nsound = 'a'\n[[on_sound]]\nsound = 'a'", "already exists"),
        ("[[on_sound]]\nactions = []", "missing 'sound'"),
        ("[[on_sound]]\nsound = 'no spaces'", "invalid sound name"),
        ("[on_silence]\nseconds = 0", "> 0"),
        ("[stop]\nmax_cycles = 0", ">= 1"),
        ("[stop]\nmax_cycles = 2.5", "whole number"),
        ("[stop]\nmax_minutes = true", "number"),
        ("[sounds.bite]\nthreshhold = 0.8", "unknown setting"),
        ("[sounds.bite]\nthreshold = 0", "> 0"),
        ("[sounds.bite]\nthreshold = 1.2", "<= 1"),
        ("[sounds.bite]\nmin_level_db = 3", "<= 0"),
        ("[sounds.bite]\ncooldown = -1", ">= 0"),
        ("[sounds.bite]\npitch_tolerance = 6", r"pitch_tolerance must be <= 0\.5 \(a fraction"),
        ("[sounds.bite]\npitch_tolerance = -0.1", r">= 0 \(a fraction of the speed: 0\.06 is"),
        ("[sounds.bite]\ntemplate_length = 30", r"template_length must be <= 10 \(seconds"),
        ("[sounds.bite]\ntemplate_length = -1", r"must be >= 0 \(seconds of each clip"),
        ("source = ", "not valid TOML"),
    ],
)
def test_errors(text, message):
    with pytest.raises(ProfileError, match=message):
        profiles.loads(text)


def test_load_error_names_the_file(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[stop]\nmax_cycles = -1\n")
    with pytest.raises(ProfileError, match=r"bad\.toml"):
        profiles.load(bad)


def test_actions_as_text():
    actions = [Wait(0.25), Key("f"), Wait(2.5), Key("9")]
    assert profiles.format_actions(actions) == "wait 0.25, f, wait 2.5, 9"
    assert profiles.parse_actions("wait 0.25, f, wait 2.5, 9") == actions
    assert profiles.parse_actions(" key space ,, wait 1 ") == [Key("space"), Wait(1.0)]
    assert profiles.parse_actions("") == []
    for bad in ("wait", "wait x", "wait 1 2", "two words", "wait -1"):
        with pytest.raises(ProfileError):
            profiles.parse_actions(bad)


def test_profile_names_live_in_the_config_folder(isolated_homes):
    assert profiles.resolve("wow") == isolated_homes / "config/sound-reactor/profiles/wow.toml"
    assert profiles.resolve("some/where.toml") == Path("some/where.toml")
    assert profiles.list_profiles() == []
    profiles.save(profiles.Profile(), profiles.resolve("b"))
    profiles.save(profiles.Profile(), profiles.resolve("a"))
    assert profiles.list_profiles() == ["a", "b"]


def test_sound_setting_bounds():
    p = profiles.loads("[sounds.bite]\nthreshold = 1.0\nmin_level_db = 0\ncooldown = 0\n")
    assert p.sounds == {"bite": {"threshold": 1.0, "min_level_db": 0.0, "cooldown": 0.0}}
    p = profiles.loads("[sounds.bite]\npitch_tolerance = 0.5\ntemplate_length = 10\n")
    assert p.sounds == {"bite": {"pitch_tolerance": 0.5, "template_length": 10.0}}
    p = profiles.loads("[sounds.bite]\npitch_tolerance = 0\ntemplate_length = 0\n")
    assert p.sounds == {"bite": {"pitch_tolerance": 0.0, "template_length": 0.0}}


def test_a_file_that_is_not_utf8(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"source = \xff\xfe\n")
    with pytest.raises(ProfileError, match="not a UTF-8 text file"):
        profiles.load(bad)


def test_a_file_with_a_byte_order_mark(tmp_path):
    # as Windows PowerShell 5.1 saves it with Set-Content -Encoding utf8
    path = tmp_path / "bom.toml"
    path.write_bytes(b'\xef\xbb\xbfsource = "app:x"\n')
    assert profiles.load(path).source == "app:x"


def test_actions_as_text_keep_exact_values():
    actions = [Wait(0.123456789), Wait(30.0), Wait(1e-05), Key("F1")]
    text = profiles.format_actions(actions)
    assert text == "wait 0.123456789, wait 30, wait 1e-05, F1"
    assert profiles.parse_actions(text) == actions
