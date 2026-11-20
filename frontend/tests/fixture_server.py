"""Serve the built app with deterministic audio for the browser smoke test."""

import os
import socket
import wave
from pathlib import Path
from unittest.mock import patch

import uvicorn

from backend.app import audio
from backend.app.main import app


def mock_voice(_text, path):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 8000)


def main():
    assemble_audio = audio._assemble_audio
    failure_marker = Path(os.environ["LIQUID_DATA_DIR"]) / "fail-assembly"

    def controlled_assembly(clips, output, ffmpeg):
        if failure_marker.exists():
            raise RuntimeError("Simulated audio assembly failure")
        assemble_audio(clips, output, ffmpeg)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        print(f"SMOKE_PORT={listener.getsockname()[1]}", flush=True)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        with (
            patch.object(audio, "offline_voice", mock_voice),
            patch.object(audio, "_assemble_audio", controlled_assembly),
        ):
            server.run(sockets=[listener])


if __name__ == "__main__":
    main()
