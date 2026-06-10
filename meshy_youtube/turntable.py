"""Render a 360° turntable of a GLB model to PNG frames using Blender.

Headless Blender (``blender --background --python``) imports the GLB, orbits a
camera around the origin, and renders one PNG per frame. Ported from the
BoTTube ``render_turntable.py`` script with the same camera rig and lighting.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
import uuid
from typing import Optional

DEFAULT_FRAMES = 180        # 6s at 30fps
DEFAULT_RESOLUTION = 720    # square

# Bounds — keep a single call from consuming unbounded CPU/disk, and stop
# frames=0 from dividing by zero in the orbit math. YouTube has no square/720
# target, so the resolution ceiling is HD (1920); frames stay capped so a
# single call can't fill the disk with PNGs.
MIN_FRAMES, MAX_FRAMES = 1, 360
MIN_RESOLUTION, MAX_RESOLUTION = 16, 1920

# Wall-clock cap so a wedged Blender can't monopolize the host forever.
BLENDER_TIMEOUT = 1800

# Cap on rendered animation frames (a Meshy clip is usually 1–4s; this bounds a
# pathologically long action so one render can't fill the disk).
MAX_ANIM_FRAMES = 600


class TurntableError(RuntimeError):
    """Blender missing, or a render that exited non-zero / produced no frames."""


def _blender_script(model_path: str, output_dir: str, frames: int,
                    resolution: int) -> str:
    # NOTE: model_path / output_dir are absolute paths we control (not user
    # free-text), interpolated into a Blender-Python script run in-process.
    return textwrap.dedent(f"""\
        import bpy
        import math
        import mathutils

        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath={model_path!r})

        cam = bpy.data.cameras.new("Camera")
        cam_obj = bpy.data.objects.new("Camera", cam)
        bpy.context.scene.collection.objects.link(cam_obj)
        bpy.context.scene.camera = cam_obj

        light = bpy.data.lights.new("Light", type="SUN")
        light.energy = 3
        light_obj = bpy.data.objects.new("Light", light)
        light_obj.location = (5, 5, 5)
        bpy.context.scene.collection.objects.link(light_obj)

        num_frames = {frames}
        # Render exactly num_frames PNGs (i = 0..num_frames-1). The final angle
        # stops just short of a full turn, so frame 0 and the last frame are not
        # duplicates — a clean loop with no seam frame.
        for i in range(num_frames):
            angle = (i / num_frames) * 2 * math.pi
            cam_obj.location = (3 * math.cos(angle), 3 * math.sin(angle), 1.5)
            cam_obj.keyframe_insert("location", frame=i)
            direction = mathutils.Vector((0, 0, 0)) - cam_obj.location
            cam_obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
            cam_obj.keyframe_insert("rotation_euler", frame=i)

        scene = bpy.context.scene
        scene.render.resolution_x = {resolution}
        scene.render.resolution_y = {resolution}
        scene.frame_start = 0
        scene.frame_end = num_frames - 1
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = {(output_dir + os.sep)!r}

        bpy.ops.render.render(animation=True)
    """)


def render(glb_path: str, output_dir: str, frames: int = DEFAULT_FRAMES,
           resolution: int = DEFAULT_RESOLUTION) -> dict:
    """Render the GLB as a turntable. Returns {frames_dir, frame_count}."""
    if not MIN_FRAMES <= frames <= MAX_FRAMES:
        raise TurntableError(
            f"frames must be in [{MIN_FRAMES}, {MAX_FRAMES}], got {frames}"
        )
    if not MIN_RESOLUTION <= resolution <= MAX_RESOLUTION:
        raise TurntableError(
            f"resolution must be in [{MIN_RESOLUTION}, {MAX_RESOLUTION}], "
            f"got {resolution}"
        )
    # Force an even dimension: H.264 + yuv420p (the ffmpeg stage) requires even
    # width/height, and an odd value would only fail *after* the billed render.
    resolution -= resolution % 2
    glb_path, output_dir = _prep_render_dir(glb_path, output_dir)
    _run_blender(_blender_script(glb_path, output_dir, frames, resolution),
                 output_dir)
    frame_count = _normalize_frame_sequence(output_dir, frames)
    return {"frames_dir": output_dir, "frame_count": frame_count}


def _prep_render_dir(glb_path: str, output_dir: str):
    """Shared pre-render checks: GLB exists, Blender present, output dir is empty
    of PNGs (we never delete a caller's images)."""
    glb_path = os.path.abspath(glb_path)
    output_dir = os.path.abspath(output_dir)
    if not os.path.isfile(glb_path):
        raise TurntableError(f"GLB model not found: {glb_path}")
    if shutil.which("blender") is None:
        raise TurntableError(
            "Blender not found in PATH. Install Blender to render "
            "(https://www.blender.org/download/)."
        )
    os.makedirs(output_dir, exist_ok=True)
    existing = [f for f in os.listdir(output_dir) if f.endswith(".png")]
    if existing:
        raise TurntableError(
            f"output_dir already contains {len(existing)} PNG file(s): "
            f"{output_dir}. Pass an empty directory so existing images are "
            f"left untouched."
        )
    return glb_path, output_dir


def _run_blender(script_text: str, output_dir: str) -> None:
    """Write a Blender-Python script to a unique file and run it headless."""
    script_path = os.path.join(output_dir, f"_render_{uuid.uuid4().hex}.py")
    with open(script_path, "w") as fh:
        fh.write(script_text)
    try:
        result = subprocess.run(
            ["blender", "--background", "--python", script_path],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,  # don't let Blender read the MCP stdio stream
            timeout=BLENDER_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise TurntableError(
            f"Blender render timed out after {BLENDER_TIMEOUT}s") from exc
    except OSError as exc:
        raise TurntableError(f"could not launch Blender: {exc}") from exc
    finally:
        if os.path.exists(script_path):
            os.remove(script_path)
    if result.returncode != 0:
        raise TurntableError(f"Blender render failed:\n{result.stderr[-2000:]}")


def _animation_script(model_path: str, output_dir: str, resolution: int,
                      max_frames: int, fallback_frames: int) -> str:
    """Blender script: import an animated GLB, frame the model with a fixed
    camera, set the scene range to the imported animation, and render it."""
    return textwrap.dedent(f"""\
        import bpy, mathutils

        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath={model_path!r})

        # Animation frame range from the imported actions (capped). Init wide
        # so an action that starts after frame 1 isn't padded with stale frames.
        fstart, fend = 10**9, -10**9
        for a in bpy.data.actions:
            fr = a.frame_range
            fstart = min(fstart, int(fr[0]))
            fend = max(fend, int(fr[1]))
        if fend < fstart:
            fstart, fend = 0, {fallback_frames} - 1
        if fend - fstart + 1 > {max_frames}:
            fend = fstart + {max_frames} - 1

        # Record the expected frame count so the host can detect a truncated
        # (crashed/interrupted) render instead of publishing a partial clip.
        with open({(output_dir + os.sep + "_expected_frames.txt")!r}, "w") as _ef:
            _ef.write(str(fend - fstart + 1))

        # World-space bounding box of all meshes, to frame the character.
        mins = [1e9, 1e9, 1e9]
        maxs = [-1e9, -1e9, -1e9]
        for obj in bpy.context.scene.objects:
            if obj.type == 'MESH':
                for corner in obj.bound_box:
                    wv = obj.matrix_world @ mathutils.Vector(corner)
                    for i in range(3):
                        mins[i] = min(mins[i], wv[i])
                        maxs[i] = max(maxs[i], wv[i])
        if mins[0] > maxs[0]:
            mins, maxs = [-1, -1, 0], [1, 1, 2]
        center = mathutils.Vector(((mins[0]+maxs[0])/2,
                                    (mins[1]+maxs[1])/2, (mins[2]+maxs[2])/2))
        size = max(maxs[0]-mins[0], maxs[1]-mins[1], maxs[2]-mins[2]) or 1.0

        cam = bpy.data.cameras.new("Camera")
        cam_obj = bpy.data.objects.new("Camera", cam)
        bpy.context.scene.collection.objects.link(cam_obj)
        bpy.context.scene.camera = cam_obj
        dist = size * 2.2
        cam_obj.location = (center[0], center[1] - dist, center[2] + size * 0.25)
        aim = center - cam_obj.location
        cam_obj.rotation_euler = aim.to_track_quat('-Z', 'Y').to_euler()

        light = bpy.data.lights.new("Light", type="SUN")
        light.energy = 3
        light_obj = bpy.data.objects.new("Light", light)
        light_obj.location = (center[0] + size, center[1] - size,
                              center[2] + size * 2)
        bpy.context.scene.collection.objects.link(light_obj)

        scene = bpy.context.scene
        scene.render.resolution_x = {resolution}
        scene.render.resolution_y = {resolution}
        scene.frame_start = fstart
        scene.frame_end = fend
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = {(output_dir + os.sep)!r}

        bpy.ops.render.render(animation=True)
    """)


def render_animation(glb_path: str, output_dir: str,
                     resolution: int = DEFAULT_RESOLUTION,
                     fallback_frames: int = 120) -> dict:
    """Render an ANIMATED GLB (from Meshy rig+animate) as a video — the model
    performs its motion in front of a fixed, auto-framed camera. The frame count
    is the animation's own length (capped at ``MAX_ANIM_FRAMES``).

    Returns {frames_dir, frame_count}."""
    if not MIN_RESOLUTION <= resolution <= MAX_RESOLUTION:
        raise TurntableError(
            f"resolution must be in [{MIN_RESOLUTION}, {MAX_RESOLUTION}], "
            f"got {resolution}")
    resolution -= resolution % 2
    glb_path, output_dir = _prep_render_dir(glb_path, output_dir)
    _run_blender(
        _animation_script(glb_path, output_dir, resolution, MAX_ANIM_FRAMES,
                          fallback_frames), output_dir)
    # The Blender script writes the count it intended to render; use it to catch
    # a truncated render (falls back to lenient if the marker is missing).
    expected = None
    marker = os.path.join(output_dir, "_expected_frames.txt")
    if os.path.isfile(marker):
        try:
            expected = int(open(marker).read().strip())
        except (ValueError, OSError):
            expected = None
        finally:
            try:
                os.remove(marker)
            except OSError:
                pass
    frame_count = _normalize_frame_sequence(output_dir, expected)
    return {"frames_dir": output_dir, "frame_count": frame_count}


def _frame_num(name: str) -> int:
    """Frame number embedded in a filename ('0007.png' -> 7), or -1 if none."""
    digits = re.sub(r"\D", "", os.path.splitext(name)[0])
    return int(digits) if digits else -1


def _normalize_frame_sequence(output_dir: str,
                              expected_count: Optional[int]) -> int:
    """Rename the PNGs in ``output_dir`` to a contiguous 0000.png, 0001.png …
    sequence in NUMERIC frame order, so ffmpeg's "%04d.png" pattern always
    matches regardless of Blender's native padding/start-number.

    ``expected_count`` enforces an exact frame count (turntables know theirs);
    pass ``None`` when the count is dynamic (an animation's length).

    Raises ``TurntableError`` on no frames, a count mismatch, or a rename error.
    """
    pngs = sorted((f for f in os.listdir(output_dir) if f.endswith(".png")),
                  key=_frame_num)
    if not pngs:
        raise TurntableError(f"no PNG frames were produced in {output_dir}")
    if expected_count is not None and len(pngs) != expected_count:
        raise TurntableError(
            f"expected {expected_count} frames but found {len(pngs)} "
            f"(likely a partial/interrupted render)"
        )
    # Two-phase via unique temp names so a rename can never clobber a
    # not-yet-moved source frame.
    try:
        temp_names = []
        for name in pngs:
            tmp = os.path.join(output_dir, f".norm_{uuid.uuid4().hex}.png")
            os.replace(os.path.join(output_dir, name), tmp)
            temp_names.append(tmp)
        for idx, tmp in enumerate(temp_names):
            os.replace(tmp, os.path.join(output_dir, f"{idx:04d}.png"))
    except OSError as exc:
        raise TurntableError(f"failed to normalize frame names: {exc}") from exc
    return len(pngs)
