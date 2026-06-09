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
    # Validate inputs (cheap) before checking for the Blender binary.
    glb_path = os.path.abspath(glb_path)
    output_dir = os.path.abspath(output_dir)
    if not os.path.isfile(glb_path):
        raise TurntableError(f"GLB model not found: {glb_path}")
    if shutil.which("blender") is None:
        raise TurntableError(
            "Blender not found in PATH. Install Blender to render turntables "
            "(https://www.blender.org/download/)."
        )
    os.makedirs(output_dir, exist_ok=True)

    # Refuse to render into a directory that already holds PNGs: we must NOT
    # delete a caller's existing images, and mixing leftovers into the frame
    # sequence would corrupt the output. Server tools always pass a fresh
    # workdir; library callers should pass an empty directory.
    existing = [f for f in os.listdir(output_dir) if f.endswith(".png")]
    if existing:
        raise TurntableError(
            f"output_dir already contains {len(existing)} PNG file(s): "
            f"{output_dir}. Pass an empty directory so existing images are "
            f"left untouched."
        )

    script_path = os.path.join(output_dir, f"_render_{uuid.uuid4().hex}.py")
    with open(script_path, "w") as fh:
        fh.write(_blender_script(glb_path, output_dir, frames, resolution))

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

    frame_count = _normalize_frame_sequence(output_dir, frames)
    return {"frames_dir": output_dir, "frame_count": frame_count}


def _frame_num(name: str) -> int:
    """Frame number embedded in a filename ('0007.png' -> 7), or -1 if none."""
    digits = re.sub(r"\D", "", os.path.splitext(name)[0])
    return int(digits) if digits else -1


def _normalize_frame_sequence(output_dir: str, expected_count: int) -> int:
    """Rename the PNGs in ``output_dir`` to a contiguous 0000.png, 0001.png …
    sequence in NUMERIC frame order, so ffmpeg's "%04d.png" pattern always
    matches regardless of Blender's native padding/start-number.

    Raises ``TurntableError`` on no frames, a count mismatch (partial render),
    or any filesystem error during the rename.
    """
    pngs = sorted((f for f in os.listdir(output_dir) if f.endswith(".png")),
                  key=_frame_num)
    if not pngs:
        raise TurntableError(f"no PNG frames were produced in {output_dir}")
    if len(pngs) != expected_count:
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
