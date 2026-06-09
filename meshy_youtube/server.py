#!/usr/bin/env python3
"""meshy-youtube MCP server.

Exposes the Meshy 3D-to-video pipeline as MCP tools that publish to YouTube:

    prompt -> Meshy text-to-3D -> Blender turntable -> ffmpeg -> videos.insert

Tools
-----
    generate_3d_model    prompt        -> .glb path + Meshy task ids
    get_meshy_task_status task_id       -> status / .glb on success
    render_turntable     .glb          -> PNG frames
    frames_to_video      frames dir    -> .mp4
    upload_to_youtube    .mp4          -> video_id / watch_url (OAuth)
    meshy_to_youtube     prompt        -> one-shot full pipeline -> watch_url

Configuration (environment):
    MESHY_API_KEY               required for Meshy generation
    YOUTUBE_CLIENT_SECRET_FILE  OAuth client json (default ~/.config/meshy-youtube-mcp/)
    YOUTUBE_TOKEN_FILE          stored token (run `python -m meshy_youtube.authorize` once)
    MESHY_YOUTUBE_WORKDIR       optional working dir (default: temp per run)
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if __name__ == "__main__":
    sys.path.insert(0, _PKG_PARENT)
else:
    try:
        import meshy_youtube  # noqa: F401
    except ImportError:
        sys.path.insert(0, _PKG_PARENT)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from meshy_youtube import meshy, turntable, video, youtube  # noqa: E402


def _load_dotenv() -> None:
    """Best-effort .env loader (source checkouts only; never site-packages or
    cwd, so an untrusted dir can't inject credentials/paths)."""
    if not os.path.isfile(os.path.join(_PKG_PARENT, "pyproject.toml")):
        return
    candidate = os.path.join(_PKG_PARENT, ".env")
    if not os.path.isfile(candidate):
        return
    try:
        with open(candidate, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


mcp = FastMCP("meshy-youtube")


def _workdir(tag: str = "") -> str:
    base = os.environ.get("MESHY_YOUTUBE_WORKDIR")
    if base:
        path = os.path.join(os.path.abspath(base), tag or uuid.uuid4().hex)
        os.makedirs(path, exist_ok=True)
        return path
    return tempfile.mkdtemp(prefix="meshy_youtube_")


def _preflight(*, need_meshy: bool = False, need_blender: bool = False,
               need_ffmpeg: bool = False, need_youtube: bool = False) -> None:
    import shutil
    missing = []
    if need_meshy and not os.environ.get("MESHY_API_KEY"):
        missing.append("MESHY_API_KEY env var")
    if need_youtube and not youtube.has_token():
        missing.append(
            f"YouTube token ({youtube.token_file()}) — run "
            f"`python -m meshy_youtube.authorize` once")
    if need_blender and shutil.which("blender") is None:
        missing.append("blender on PATH")
    if need_ffmpeg and (shutil.which("ffmpeg") is None
                        or shutil.which("ffprobe") is None):
        missing.append("ffmpeg/ffprobe on PATH")
    if missing:
        raise RuntimeError("preflight failed — missing: " + ", ".join(missing))


def _tags_list(tags: str) -> list:
    return [t.strip() for t in tags.split(",") if t.strip()] if tags else []


@mcp.tool()
def generate_3d_model(prompt: str, art_style: str = "realistic",
                      should_remesh: bool = True, texture_prompt: str = "",
                      enable_pbr: bool = True, timeout: int = 600) -> dict:
    """Generate a 3D model from a text prompt via Meshy.ai (preview → refine).
    The refine stage textures the model: enable_pbr (default True) for PBR
    textures, texture_prompt for extra guidance. Blocks until ready; returns the
    local .glb path and Meshy task ids."""
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    out = os.path.join(_workdir(), "model.glb")
    return meshy.generate(prompt, out, art_style=art_style,
                          should_remesh=should_remesh,
                          texture_prompt=texture_prompt or None,
                          enable_pbr=enable_pbr, timeout=timeout)


@mcp.tool()
def get_meshy_task_status(task_id: str, download: bool = False) -> dict:
    """Inspect a Meshy task; optionally download the .glb on success."""
    status = meshy.get_task(task_id)
    state = status.get("status", "UNKNOWN")
    result = {"task_id": task_id, "status": state,
              "progress": status.get("progress", 0),
              "model_urls": status.get("model_urls", {})}
    if state == "SUCCEEDED" and download and (status.get("model_urls") or {}).get("glb"):
        result["glb_path"] = meshy.download_glb(
            status, os.path.join(_workdir(), "model.glb"))
    return result


@mcp.tool()
def render_turntable(glb_path: str, frames: int = 180,
                     resolution: int = 1080) -> dict:
    """Render a GLB as a 360° turntable to PNG frames (requires Blender).
    Defaults to 1080² — YouTube has no 720² constraint like BoTTube does."""
    return turntable.render(glb_path, _workdir(), frames=frames,
                            resolution=resolution)


@mcp.tool()
def frames_to_video(frames_dir: str, fps: int = 30, duration: int = 6) -> dict:
    """Combine numbered PNG frames into an H.264 mp4 (YouTube-ready as-is)."""
    out = os.path.join(_workdir(), "turntable.mp4")
    return {"video_path": video.frames_to_video(frames_dir, out, fps=fps,
                                                duration=duration)}


@mcp.tool()
def upload_to_youtube(video_path: str, title: str, description: str = "",
                      tags: str = "", privacy: str = "unlisted",
                      category_id: str = "22",
                      made_for_kids: bool = False) -> dict:
    """Upload a finished video to YouTube (OAuth). tags is comma-separated.
    privacy: public | unlisted | private (default unlisted).
    made_for_kids: COPPA audience flag — set True only for child-directed content.

    Note: uploads whatever local path you point it at, under your own YouTube
    account — intentional, so you can publish videos made elsewhere."""
    _preflight(need_youtube=True)
    return youtube.upload(video_path, title, description=description,
                          tags=_tags_list(tags), privacy=privacy,
                          category_id=category_id, made_for_kids=made_for_kids)


@mcp.tool()
def meshy_to_youtube(prompt: str, title: str, description: str = "",
                     tags: str = "3d,meshy,turntable",
                     privacy: str = "unlisted", category_id: str = "22",
                     made_for_kids: bool = False,
                     art_style: str = "realistic", should_remesh: bool = True,
                     texture_prompt: str = "", enable_pbr: bool = True,
                     frames: int = 180, resolution: int = 1080, fps: int = 30,
                     duration: int = 6, timeout: int = 600) -> dict:
    """One-shot: prompt -> Meshy 3D -> turntable -> video -> YouTube upload.

    Always returns a dict. ``ok=True`` with ``watch_url`` on success; on a known
    stage failure ``ok=False`` with ``error``/``failed_stage`` and whatever
    artifacts were already produced.
    """
    steps: dict = {"prompt": prompt, "ok": False}
    stage = "validate"
    try:
        if art_style not in meshy.ART_STYLES:
            raise ValueError(f"art_style must be one of {meshy.ART_STYLES}, "
                             f"got {art_style!r}")
        if privacy not in youtube.VALID_PRIVACY:
            raise ValueError(f"privacy must be one of {youtube.VALID_PRIVACY}, "
                             f"got {privacy!r}")
        if not turntable.MIN_FRAMES <= frames <= turntable.MAX_FRAMES:
            raise ValueError(f"frames must be in [{turntable.MIN_FRAMES}, "
                             f"{turntable.MAX_FRAMES}], got {frames}")
        if not turntable.MIN_RESOLUTION <= resolution <= turntable.MAX_RESOLUTION:
            raise ValueError(f"resolution must be in [{turntable.MIN_RESOLUTION}, "
                             f"{turntable.MAX_RESOLUTION}], got {resolution}")
        if fps < 1 or duration < 1:
            raise ValueError(f"fps and duration must be >= 1 (got fps={fps}, "
                             f"duration={duration})")
        if frames < fps * duration:
            raise ValueError(
                f"frames ({frames}) < fps*duration ({fps * duration}); the "
                f"video would be shorter than {duration}s")
        if not title or not title.strip():
            raise ValueError("title must be a non-empty string")
        if timeout < 1:
            raise ValueError(f"timeout must be >= 1, got {timeout}")

        stage = "preflight"
        _preflight(need_meshy=True, need_blender=True, need_ffmpeg=True,
                   need_youtube=True)
        stage = "workdir"
        work = _workdir()
        steps["workdir"] = work

        stage = "meshy"
        glb = meshy.generate(prompt, os.path.join(work, "model.glb"),
                             art_style=art_style, should_remesh=should_remesh,
                             texture_prompt=texture_prompt or None,
                             enable_pbr=enable_pbr, timeout=timeout)
        steps["glb_path"] = glb["glb_path"]

        stage = "turntable"
        tt = turntable.render(glb["glb_path"], os.path.join(work, "frames"),
                              frames=frames, resolution=resolution)
        steps["frame_count"] = tt["frame_count"]
        steps["frames_dir"] = tt["frames_dir"]

        stage = "frames_to_video"
        raw = video.frames_to_video(tt["frames_dir"],
                                    os.path.join(work, "turntable.mp4"),
                                    fps=fps, duration=duration)
        steps["video_path"] = raw

        stage = "upload"
        up = youtube.upload(raw, title, description=description,
                            tags=_tags_list(tags), privacy=privacy,
                            category_id=category_id, made_for_kids=made_for_kids)
        steps["upload"] = up
        steps["watch_url"] = up.get("watch_url")
        steps["ok"] = True
        return steps
    except Exception as exc:  # noqa: BLE001 — one-shot always returns a dict
        steps["error"] = f"{type(exc).__name__}: {exc}"
        steps["failed_stage"] = stage
        return steps


# --- shared one-shot helpers ---------------------------------------------

def _validate_publish_params(*, title: str, frames: int, resolution: int,
                             fps: int, duration: int, timeout: int) -> None:
    """Cheap param validation shared by the one-shot tools (runs before any
    billed Meshy work). YouTube has no 8s/720 cap, so duration is unbounded."""
    if not turntable.MIN_FRAMES <= frames <= turntable.MAX_FRAMES:
        raise ValueError(f"frames must be in [{turntable.MIN_FRAMES}, "
                         f"{turntable.MAX_FRAMES}], got {frames}")
    if not turntable.MIN_RESOLUTION <= resolution <= turntable.MAX_RESOLUTION:
        raise ValueError(f"resolution must be in [{turntable.MIN_RESOLUTION}, "
                         f"{turntable.MAX_RESOLUTION}], got {resolution}")
    if fps < 1 or duration < 1:
        raise ValueError(f"fps and duration must be >= 1 (got fps={fps}, "
                         f"duration={duration})")
    if frames < fps * duration:
        raise ValueError(f"frames ({frames}) < fps*duration ({fps * duration}); "
                         f"video would be shorter than {duration}s")
    if not title or not title.strip():
        raise ValueError("title must be a non-empty string")
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")


def _render_and_publish(work: str, glb_path: str, *, title: str,
                        description: str, tags: str, privacy: str,
                        category_id: str, made_for_kids: bool, frames: int,
                        resolution: int, fps: int, duration: int,
                        steps: dict) -> dict:
    """Shared tail: GLB -> turntable -> video -> YouTube upload (no 720 prep)."""
    tt = turntable.render(glb_path, os.path.join(work, "frames"),
                          frames=frames, resolution=resolution)
    steps["frame_count"] = tt["frame_count"]
    steps["frames_dir"] = tt["frames_dir"]
    raw = video.frames_to_video(tt["frames_dir"],
                                os.path.join(work, "turntable.mp4"),
                                fps=fps, duration=duration)
    steps["video_path"] = raw
    up = youtube.upload(raw, title, description=description,
                        tags=_tags_list(tags), privacy=privacy,
                        category_id=category_id, made_for_kids=made_for_kids)
    steps["upload"] = up
    steps["watch_url"] = up.get("watch_url")
    return up


@mcp.tool()
def generate_3d_from_image(image: str, texture_prompt: str = "",
                           enable_pbr: bool = True, should_texture: bool = True,
                           should_remesh: bool = True, timeout: int = 600) -> dict:
    """Image-to-3D: a photo/render (public URL or local file path) -> textured
    .glb. Returns the local .glb path and the Meshy task id."""
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    out = os.path.join(_workdir(), "model.glb")
    return meshy.generate_from_image(
        image, out, enable_pbr=enable_pbr, should_texture=should_texture,
        should_remesh=should_remesh, texture_prompt=texture_prompt or None,
        timeout=timeout)


@mcp.tool()
def generate_3d_from_images(images: list, texture_prompt: str = "",
                            enable_pbr: bool = True, should_texture: bool = True,
                            should_remesh: bool = True, timeout: int = 600) -> dict:
    """Multi-image-to-3D: 1-4 reference images (URLs or local paths) of one
    subject -> a higher-fidelity textured .glb."""
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    out = os.path.join(_workdir(), "model.glb")
    return meshy.generate_from_images(
        images, out, enable_pbr=enable_pbr, should_texture=should_texture,
        should_remesh=should_remesh, texture_prompt=texture_prompt or None,
        timeout=timeout)


@mcp.tool()
def image_to_youtube(image: str, title: str, description: str = "",
                     tags: str = "3d,meshy,turntable", privacy: str = "unlisted",
                     category_id: str = "22", made_for_kids: bool = False,
                     enable_pbr: bool = True, should_texture: bool = True,
                     should_remesh: bool = True, frames: int = 180,
                     resolution: int = 1080, fps: int = 30, duration: int = 6,
                     timeout: int = 600) -> dict:
    """One-shot: an image -> Meshy image-to-3D -> turntable -> YouTube video.
    Always returns a dict (ok + watch_url, or ok=False + error/failed_stage)."""
    steps: dict = {"source_image": image, "ok": False}
    stage = "validate"
    try:
        _validate_publish_params(title=title, frames=frames, resolution=resolution,
                                 fps=fps, duration=duration, timeout=timeout)
        if privacy not in youtube.VALID_PRIVACY:
            raise ValueError(f"privacy must be one of {youtube.VALID_PRIVACY}")
        stage = "preflight"
        _preflight(need_meshy=True, need_blender=True, need_ffmpeg=True,
                   need_youtube=True)
        stage = "workdir"
        work = _workdir()
        steps["workdir"] = work
        stage = "meshy"
        glb = meshy.generate_from_image(
            image, os.path.join(work, "model.glb"), enable_pbr=enable_pbr,
            should_texture=should_texture, should_remesh=should_remesh,
            timeout=timeout)
        steps["glb_path"] = glb["glb_path"]
        stage = "render_publish"
        _render_and_publish(work, glb["glb_path"], title=title,
                            description=description, tags=tags, privacy=privacy,
                            category_id=category_id, made_for_kids=made_for_kids,
                            frames=frames, resolution=resolution, fps=fps,
                            duration=duration, steps=steps)
        steps["ok"] = True
        return steps
    except Exception as exc:  # noqa: BLE001 — one-shot always returns a dict
        steps["error"] = f"{type(exc).__name__}: {exc}"
        steps["failed_stage"] = stage
        return steps


@mcp.tool()
def retexture_model(text_style_prompt: str = "", image_style_url: str = "",
                    input_task_id: str = "", model_url: str = "",
                    enable_pbr: bool = True, timeout: int = 600) -> dict:
    """Re-texture an existing model into a new variant. Identify the source by
    input_task_id (a prior Meshy task) or a public model_url; describe the look
    with text_style_prompt or image_style_url. Returns the new .glb path."""
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    out = os.path.join(_workdir(), "model.glb")
    return meshy.retexture(
        out, input_task_id=input_task_id or None, model_url=model_url or None,
        text_style_prompt=text_style_prompt or None,
        image_style_url=image_style_url or None, enable_pbr=enable_pbr,
        timeout=timeout)


@mcp.tool()
def retexture_to_youtube(title: str, text_style_prompt: str = "",
                         image_style_url: str = "", input_task_id: str = "",
                         model_url: str = "", description: str = "",
                         tags: str = "3d,meshy,retexture",
                         privacy: str = "unlisted", category_id: str = "22",
                         made_for_kids: bool = False, enable_pbr: bool = True,
                         frames: int = 180, resolution: int = 1080,
                         fps: int = 30, duration: int = 6,
                         timeout: int = 600) -> dict:
    """One-shot: re-texture an existing model -> turntable -> YouTube video.
    Always returns a dict."""
    steps: dict = {"ok": False}
    stage = "validate"
    try:
        _validate_publish_params(title=title, frames=frames, resolution=resolution,
                                 fps=fps, duration=duration, timeout=timeout)
        if privacy not in youtube.VALID_PRIVACY:
            raise ValueError(f"privacy must be one of {youtube.VALID_PRIVACY}")
        stage = "preflight"
        _preflight(need_meshy=True, need_blender=True, need_ffmpeg=True,
                   need_youtube=True)
        stage = "workdir"
        work = _workdir()
        steps["workdir"] = work
        stage = "meshy"
        glb = meshy.retexture(
            os.path.join(work, "model.glb"), input_task_id=input_task_id or None,
            model_url=model_url or None,
            text_style_prompt=text_style_prompt or None,
            image_style_url=image_style_url or None, enable_pbr=enable_pbr,
            timeout=timeout)
        steps["glb_path"] = glb["glb_path"]
        stage = "render_publish"
        _render_and_publish(work, glb["glb_path"], title=title,
                            description=description, tags=tags, privacy=privacy,
                            category_id=category_id, made_for_kids=made_for_kids,
                            frames=frames, resolution=resolution, fps=fps,
                            duration=duration, steps=steps)
        steps["ok"] = True
        return steps
    except Exception as exc:  # noqa: BLE001 — one-shot always returns a dict
        steps["error"] = f"{type(exc).__name__}: {exc}"
        steps["failed_stage"] = stage
        return steps


@mcp.tool()
def rig_model(input_task_id: str = "", model_url: str = "",
              height_meters: float = 1.7, timeout: int = 600) -> dict:
    """Auto-rig a humanoid model for animation. Identify it by input_task_id (a
    prior Meshy generation) or a public model_url. Returns rig_task_id."""
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    return meshy.rig(input_task_id=input_task_id or None,
                     model_url=model_url or None, height_meters=height_meters,
                     timeout=timeout)


@mcp.tool()
def animate_model(rig_task_id: str, action_id: int, fps: int = 30,
                  timeout: int = 600) -> dict:
    """Apply a motion to a rigged model -> animated .glb. action_id is from
    Meshy's library (e.g. 0=Idle, 1=Walking, 4=Attack, 22=Dancing)."""
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    out = os.path.join(_workdir(), "anim.glb")
    return meshy.animate(rig_task_id, action_id, out,
                         fps=fps if fps in (24, 25, 30, 60) else None,
                         timeout=timeout)


@mcp.tool()
def animate_to_youtube(action_id: int, title: str, input_task_id: str = "",
                       model_url: str = "", description: str = "",
                       tags: str = "3d,meshy,animation", privacy: str = "unlisted",
                       category_id: str = "22", made_for_kids: bool = False,
                       height_meters: float = 1.7, fps: int = 30,
                       resolution: int = 1080, timeout: int = 600) -> dict:
    """One-shot: a humanoid model -> Meshy rig -> animate (action_id) -> render
    the MOTION -> YouTube. The clip shows the character performing the action;
    its length follows the animation. Always returns a dict."""
    steps: dict = {"action_id": action_id, "ok": False}
    stage = "validate"
    try:
        if not (input_task_id or model_url):
            raise ValueError("provide input_task_id or model_url")
        if not isinstance(action_id, int) or action_id < 0:
            raise ValueError("action_id must be a non-negative integer")
        if not turntable.MIN_RESOLUTION <= resolution <= turntable.MAX_RESOLUTION:
            raise ValueError(f"resolution must be in [{turntable.MIN_RESOLUTION}, "
                             f"{turntable.MAX_RESOLUTION}], got {resolution}")
        if fps < 1:
            raise ValueError(f"fps must be >= 1, got {fps}")
        if privacy not in youtube.VALID_PRIVACY:
            raise ValueError(f"privacy must be one of {youtube.VALID_PRIVACY}")
        if not title or not title.strip():
            raise ValueError("title must be a non-empty string")
        if timeout < 1:
            raise ValueError(f"timeout must be >= 1, got {timeout}")

        stage = "preflight"
        _preflight(need_meshy=True, need_blender=True, need_ffmpeg=True,
                   need_youtube=True)
        stage = "workdir"
        work = _workdir()
        steps["workdir"] = work
        stage = "rigging"
        rigged = meshy.rig(input_task_id=input_task_id or None,
                           model_url=model_url or None,
                           height_meters=height_meters, timeout=timeout)
        steps["rig_task_id"] = rigged["rig_task_id"]
        stage = "animation"
        anim = meshy.animate(rigged["rig_task_id"], action_id,
                             os.path.join(work, "anim.glb"),
                             fps=fps if fps in (24, 25, 30, 60) else None,
                             timeout=timeout)
        steps["glb_path"] = anim["glb_path"]
        stage = "render"
        tt = turntable.render_animation(anim["glb_path"],
                                        os.path.join(work, "frames"),
                                        resolution=resolution)
        steps["frame_count"] = tt["frame_count"]
        steps["frames_dir"] = tt["frames_dir"]
        stage = "frames_to_video"
        duration = max(1, round(tt["frame_count"] / max(1, fps)))
        raw = video.frames_to_video(tt["frames_dir"],
                                    os.path.join(work, "anim.mp4"),
                                    fps=fps, duration=duration)
        steps["video_path"] = raw
        stage = "upload"
        up = youtube.upload(raw, title, description=description,
                            tags=_tags_list(tags), privacy=privacy,
                            category_id=category_id, made_for_kids=made_for_kids)
        steps["upload"] = up
        steps["watch_url"] = up.get("watch_url")
        steps["ok"] = True
        return steps
    except Exception as exc:  # noqa: BLE001 — one-shot always returns a dict
        steps["error"] = f"{type(exc).__name__}: {exc}"
        steps["failed_stage"] = stage
        return steps


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    _load_dotenv()
    mcp.run()


if __name__ == "__main__":
    main()
