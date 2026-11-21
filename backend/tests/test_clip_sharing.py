import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.audio import clip_plan, create_audio
from backend.app.pipeline import _NARRATION_FRAMES, introduce_once

INTRO = _NARRATION_FRAMES["en"]["intermediate"]


def segment(story, text):
    return {"id": f"segment-{story}", "text": f"{INTRO} {text}", "fact_ids": [f"fact-{story}"], "sources": [], "conflict": False}


def voice(text, path, language="en"):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x01\x00" * 8)


class ClipSharingTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SimpleNamespace(audio_dir=Path(directory.name), record_usage=lambda *args, **kwargs: None)
        self.rate, self.jobs = segment("rate", "Policy rate, 3.25 percent."), segment("jobs", "Workers, 8,600.")
        self.stories = [{"id": "rate", "title": "Bank holds rate"}, {"id": "jobs", "title": "Ports reach wage deal"}]

    def briefing(self, *segments):
        return {"language": "en", "stories": self.stories, "segments": introduce_once(list(segments))}

    def test_a_story_clip_is_the_same_wherever_the_story_sits_in_a_briefing(self):
        first, second = self.briefing(self.rate, self.jobs), self.briefing(self.jobs, self.rate)
        plans = [clip_plan(briefing, None, "demo") for briefing in (first, second)]
        self.assertEqual([kind for kind, _, _ in plans[0]], ["intro", "story", "story"])
        self.assertEqual(plans[0][0], plans[1][0])                                # one opening line for both
        self.assertEqual({key for _, _, key in plans[0]}, {key for _, _, key in plans[1]})
        self.assertEqual(plans[0][1][1], "Bank holds rate. Policy rate, 3.25 percent.")

        with patch("backend.app.audio.offline_voice", side_effect=voice) as spoken:
            urls = [create_audio(briefing, self.store, None, "demo") for briefing in (first, second, first)]
        self.assertEqual(spoken.call_count, 3)   # the opening line and two stories, for three briefings
        self.assertNotEqual(urls[0], urls[1])
        self.assertEqual(urls[0], urls[2])

    def test_voices_and_languages_never_share_clips(self):
        briefing = self.briefing(self.rate)
        keys = lambda voice_id, language="en": {key for _, _, key in clip_plan(
            dict(briefing, language=language), SimpleNamespace(voice_id=lambda: voice_id), "live")}
        self.assertEqual(keys("daniel"), keys("daniel"))
        self.assertFalse(keys("daniel") & keys("lily"))
        self.assertFalse(keys("daniel") & keys("daniel", "de"))


if __name__ == "__main__":
    unittest.main()
