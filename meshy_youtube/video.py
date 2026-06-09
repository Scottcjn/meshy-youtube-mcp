"""ffmpeg stage: numbered PNG frames -> an H.264 mp4 ready for YouTube.

YouTube re-encodes on ingest and has no square/8s constraint, so (unlike the
BoTTube edition) there is no 720x720 / duration-cap "prepare" step — the
turntable mp4 uploads directly.
"""
from __future__ import annotations

import os
import shutil
import subprocess

# Wall-clock cap so a wedged ffmpeg can't hang the host.
FFMPEG_TIMEOUT = 600


class VideoError(RuntimeError):
    """ffmpeg missing or a non-zero exit."""


def _require(tool: str) -> None:
    if shutil.which(tool) is None:
        raise VideoError(f"{tool} not found in PATH. Install ffmpeg.")


def _run(cmd: list[str]) -> None:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            stdin=subprocess.DEVNULL,  # keep ffmpeg off the MCP stdio stream
            timeout=FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"{cmd[0]} timed out after {FFMPEG_TIMEOUT}s") from exc
    except OSError as exc:
        raise VideoError(f"could not launch {cmd[0]}: {exc}") from exc
    if result.returncode != 0:
        raise VideoError(f"{cmd[0]} failed:\n{result.stderr[-2000:]}")


def frames_to_video(frames_dir: str, output_path: str, fps: int = 30,
                    duration: int = 6, pattern: str = "%04d.png") -> str:
    """Combine numbered PNG frames into an H.264 mp4."""
    if fps < 1 or duration < 1:
        raise VideoError(f"fps and duration must be >= 1 (got fps={fps}, "
                         f"duration={duration})")
    _require("ffmpeg")
    frames_dir = os.path.abspath(frames_dir)
    if not os.path.isdir(frames_dir):
        raise VideoError(f"frames directory not found: {frames_dir}")
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    _run([
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", os.path.join(frames_dir, pattern),
        "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-an",
        output_path,
    ])
    return output_path
