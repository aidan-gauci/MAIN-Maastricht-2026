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
    subprocess.run(command, input=text, encoding="utf-8", env=env, capture_output=True, check=True, timeout=90)


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
            store.record_usage("speech_generation", mode="demo", characters=len(text), estimated_usd=0)
        if not temporary.is_file() or temporary.stat().st_size <= 44:
            raise RuntimeError("Empty audio")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return f"/api/audio/{target.name}"
