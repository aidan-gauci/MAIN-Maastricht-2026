import hashlib
import json
import os
import shutil
import subprocess
import wave
from contextlib import contextmanager
from uuid import uuid4

from .providers import voice_settings


STORY_GAP_SECONDS = 0.9


def offline_voice(text, path):
    if binary := (shutil.which("espeak-ng") or shutil.which("espeak")):
        command = [binary, "-v", "en", "-w", path.name, "--stdin"]
        env = None
        working_dir = path.parent
    elif os.name == "nt":
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.Speech; "
            "$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$voice.SetOutputToWaveFile($env:LIQUID_VOICE_OUTPUT); "
            "$voice.Speak([Console]::In.ReadToEnd()); $voice.Dispose()",
        ]
        env = {**os.environ, "LIQUID_VOICE_OUTPUT": str(path)}
        working_dir = None
    else:
        raise RuntimeError("Install eSpeak NG for offline narration")
    subprocess.run(command, input=text, encoding="utf-8", env=env, capture_output=True, check=True, timeout=speech_timeout(),
                   cwd=working_dir)


def speech_timeout():
    """Seconds one narration may take before the briefing falls back to transcript-only."""
    try:
        return max(5.0, float(os.environ.get("LIQUID_SPEECH_TIMEOUT_SECONDS", "60")))
    except ValueError:
        return 60.0


def audio_file(store, audio_url):
    """Return the local file behind an /api/audio URL, or None when it is missing."""
    if not audio_url:
        return None
    path = store.audio_dir / str(audio_url).rsplit("/", 1)[-1]
    return path if path.is_file() else None


def _audio_key(value):
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@contextmanager
def _audio_lock(target):
    # Shard the file locks to limit their number and share them across workers.
    lock_path = target.with_name(f".audio-{target.stem[:2]}.lock")
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _publish_audio(target, render):
    if target.is_file():
        return False
    with _audio_lock(target):
        if target.is_file():
            return False
        temporary = target.with_name(f"{uuid4().hex}.tmp{target.suffix}")
        try:
            render(temporary)
            if not temporary.is_file() or temporary.stat().st_size <= 44:
                raise RuntimeError("Empty audio")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return True


def _assemble_audio(clips, output, ffmpeg):
    if output.suffix == ".wav":
        with wave.open(str(output), "wb") as combined:
            for index, clip in enumerate(clips):
                with wave.open(str(clip), "rb") as fragment:
                    params = fragment.getparams()._replace(nframes=0)
                    if index == 0:
                        format_params = params
                        combined.setparams(params)
                    elif params != format_params:
                        raise ValueError("Incompatible WAV segments")
                    while frames := fragment.readframes(65536):
                        combined.writeframesraw(frames)
        return

    # A short silence between stories makes the briefing easier to follow. Re-encoding (instead of
    # stream copy) keeps the join valid whatever the clips' channel layout.
    gap = output.with_name(f"{output.stem}.gap.mp3")
    manifest = output.with_suffix(".txt")
    try:
        subprocess.run(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", str(STORY_GAP_SECONDS), "-b:a", "128k", str(gap)],
            capture_output=True, check=True, timeout=30,
        )
        entries = []
        for index, clip in enumerate(clips):
            if index:
                entries.append(f"file {gap.name}\n")
            entries.append(f"file {clip.name}\n")
        manifest.write_text("".join(entries), encoding="ascii")
        subprocess.run(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-f", "concat", "-i", str(manifest),
             "-ac", "1", "-ar", "44100", "-b:a", "128k", str(output)],
            capture_output=True, check=True, timeout=90,
        )
    finally:
        manifest.unlink(missing_ok=True)
        gap.unlink(missing_ok=True)


def create_audio(briefing, store, providers, mode):
    """Reuse synthesized segments and assemble a single file for the existing player."""
    extension = "mp3" if mode == "live" else "wav"
    language = briefing["language"]
    voice = (
        os.getenv("ELEVENLABS_VOICE_ID", ""), os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2"), "pauses-v1",
        sorted(voice_settings(getattr(providers, "expressiveness", "balanced")).items()),
    ) if mode == "live" else ("offline",)
    if not briefing["segments"]:
        raise ValueError("A briefing needs at least one audio segment")
    # The voice announces each story by its headline; the transcript already shows it as a heading.
    from .pipeline import spoken_text
    headlines = {"segment-" + str(story.get("id")): story.get("title", "") for story in briefing.get("stories", [])}
    segments = [dict(segment, text=spoken_text(segment, headlines.get(segment.get("id"), ""), language))
                for segment in briefing["segments"]]
    clip_keys = [_audio_key(("segment-v1", mode, voice, language, segment)) for segment in segments]
    clips = [store.audio_dir / f"{key}.{extension}" for key in clip_keys]
    if len(clips) == 1:
        target = clips[0]
    else:
        key = _audio_key(("briefing-v1", [clip.name for clip in clips]))
        target = store.audio_dir / f"{key}.{extension}"
    url = f"/api/audio/{target.name}"
    store.audio_dir.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        return url

    ffmpeg = None
    if mode == "live" and len(clips) > 1:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("Install FFmpeg to assemble live audio")

    for segment, clip in zip(segments, clips):
        def synthesize(output):
            if mode == "live":
                providers.synthesize(segment["text"], output, language)
            else:
                offline_voice(segment["text"], output)

        if _publish_audio(clip, synthesize) and mode == "demo":
            store.record_usage("speech_generation", mode=mode, characters=len(segment["text"]), estimated_usd=0)
    if len(clips) > 1:
        _publish_audio(target, lambda output: _assemble_audio(clips, output, ffmpeg))
    return url
