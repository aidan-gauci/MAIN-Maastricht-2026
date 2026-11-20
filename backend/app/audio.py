import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4


def offline_voice(text, path):
    if binary := (shutil.which("espeak-ng") or shutil.which("espeak")):
        command = [binary, "-v", "en", "-w", str(path), "--stdin"]
        env = None
    elif os.name == "nt":
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.Speech; "
            "$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$voice.SetOutputToWaveFile($env:LIQUID_VOICE_OUTPUT); "
            "$voice.Speak([Console]::In.ReadToEnd()); $voice.Dispose()",
        ]
        env = {**os.environ, "LIQUID_VOICE_OUTPUT": str(path)}
    else:
        raise RuntimeError("Install eSpeak NG for offline narration")
    subprocess.run(command, input=text, encoding="utf-8", env=env, capture_output=True, check=True, timeout=speech_timeout())


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
    path = Path(store.audio_dir) / str(audio_url).rsplit("/", 1)[-1]
    return path if path.is_file() else None


def create_audio(briefing, store, providers, mode):
    extension = "mp3" if mode == "live" else "wav"
    target = Path(store.audio_dir) / f"{briefing['id']}.{extension}"
    temporary = target.with_name(f"{uuid4().hex}.{extension}")
    text = "\n\n".join(segment["text"] for segment in briefing["segments"])
    try:
        if mode == "live":
            providers.synthesize(text, temporary, briefing["language"])
        else:
            offline_voice(text, temporary)
        if not temporary.is_file() or temporary.stat().st_size <= 44:
            raise RuntimeError("Empty audio")
        temporary.replace(target)
        if mode != "live":
            # Count only narrations that produced playable audio (live usage is recorded by the provider).
            store.record_usage("speech_generation", mode=mode, characters=len(text), estimated_usd=0)
    finally:
        temporary.unlink(missing_ok=True)
    return f"/api/audio/{target.name}"
