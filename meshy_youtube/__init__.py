"""meshy_youtube — the Meshy 3D-to-video pipeline, published to YouTube.

Same generation stages as the BoTTube edition (text prompt -> Meshy 3D ->
Blender turntable -> ffmpeg), with a YouTube Data API v3 publisher instead of
BoTTube's upload endpoint.

    meshy      text prompt   -> Meshy.ai text-to-3D  -> .glb model
    turntable  .glb model    -> Blender 360° orbit   -> PNG frames
    video      PNG frames    -> ffmpeg               -> .mp4
    youtube    .mp4          -> videos.insert (OAuth) -> published video
"""

__version__ = "0.1.0"

from . import meshy, turntable, video, youtube  # noqa: F401

__all__ = ["meshy", "turntable", "video", "youtube", "__version__"]
