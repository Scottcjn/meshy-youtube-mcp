"""Meshy.ai text-to-3D generation (two-stage preview → refine).

Meshy's text-to-3D API is two tasks: a ``preview`` task turns a prompt into a
base mesh, then a ``refine`` task (referencing the preview's id) textures it.
``generate`` runs both, polls each to completion, and downloads the final GLB.

This is the API-correct flow and works on any Meshy plan. (BoTTube historically
issued a single bare ``mode="refine"`` call; that path is not reproduced here
because refine requires a completed ``preview_task_id``.)
"""
from __future__ import annotations

import base64
import mimetypes
import os
import re
import tempfile
import time
from typing import Callable, Optional

import requests

_API_ROOT = "https://api.meshy.ai/openapi"
MESHY_BASE = f"{_API_ROOT}/v2/text-to-3d"  # default endpoint (text-to-3D)

# Other Meshy task endpoints (all share the create -> poll -> model_urls.glb
# shape, so the helpers below take an ``endpoint`` argument).
EP_TEXT_TO_3D = MESHY_BASE
EP_IMAGE_TO_3D = f"{_API_ROOT}/v1/image-to-3d"
EP_MULTI_IMAGE_TO_3D = f"{_API_ROOT}/v1/multi-image-to-3d"
EP_RETEXTURE = f"{_API_ROOT}/v1/retexture"
EP_RIGGING = f"{_API_ROOT}/v1/rigging"
EP_ANIMATION = f"{_API_ROOT}/v1/animation"

# Meshy task ids are uuid-like; this allowlist stops a crafted id from doing
# path traversal or query injection into the authenticated status URL.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

ART_STYLES = ("realistic", "cartoon", "low-poly", "sculpture")

# Network timeouts (seconds). Generation progress is handled by polling, not by
# a single long HTTP call, so these stay short and fail fast on a dead network.
_HTTP_TIMEOUT = 60
_DOWNLOAD_TIMEOUT = 300
# Consecutive transient poll failures tolerated before abandoning a wait.
_MAX_POLL_FAILURES = 4
_MAX_GLB_BYTES = 200 * 1024 * 1024  # 200 MB sanity cap on a downloaded model

# Progress callback: (stage, state, percent) -> None
ProgressFn = Callable[[str, str, int], None]


class MeshyError(RuntimeError):
    """Any failure talking to Meshy, or a task that ends in FAILED."""


def _headers() -> dict:
    key = os.environ.get("MESHY_API_KEY")
    if not key:
        raise MeshyError(
            "MESHY_API_KEY environment variable not set. "
            "Get a key at https://www.meshy.ai/"
        )
    return {"Authorization": f"Bearer {key}"}


def _check_endpoint(endpoint: str) -> None:
    """Only ever send the bearer token to the real Meshy API — an arbitrary
    ``endpoint`` must not be able to exfiltrate credentials."""
    if not endpoint.startswith(_API_ROOT + "/"):
        raise MeshyError(
            f"refusing to send credentials to a non-Meshy endpoint: {endpoint!r}")


def _create(payload: dict, endpoint: str = MESHY_BASE) -> str:
    _check_endpoint(endpoint)
    try:
        resp = requests.post(endpoint, headers=_headers(), json=payload,
                            timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise MeshyError(f"Meshy create request failed: {exc}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise MeshyError(f"Meshy returned non-JSON: {resp.text[:200]}") from exc
    if not isinstance(data, dict):
        raise MeshyError(f"Meshy returned unexpected JSON shape: {resp.text[:200]}")
    result = data.get("result")
    if not isinstance(result, str) or not result:
        raise MeshyError(f"Meshy did not return a task id: {resp.text[:200]}")
    return result


def create_preview_task(prompt: str, art_style: str = "realistic",
                        should_remesh: bool = True) -> str:
    """Create a preview (base-mesh) task and return its id."""
    if art_style not in ART_STYLES:
        raise MeshyError(f"art_style must be one of {ART_STYLES}, got {art_style!r}")
    if not prompt or not prompt.strip():
        raise MeshyError("prompt must be a non-empty string")
    return _create({
        "mode": "preview",
        "prompt": prompt,
        "art_style": art_style,
        "should_remesh": should_remesh,
    })


def create_refine_task(preview_task_id: str, texture_prompt: Optional[str] = None,
                       enable_pbr: bool = True) -> str:
    """Create a refine (texturing) task for a completed preview task.

    The refine stage is what TEXTURES the base mesh. ``enable_pbr`` produces
    physically-based textures (default True); ``texture_prompt`` gives the
    texturing stage extra guidance (e.g. "weathered bronze, mossy")."""
    if not preview_task_id:
        raise MeshyError("preview_task_id is required to refine")
    payload = {"mode": "refine", "preview_task_id": preview_task_id,
               "enable_pbr": bool(enable_pbr)}
    if texture_prompt:
        payload["texture_prompt"] = texture_prompt
    return _create(payload)


def get_task(task_id: str, endpoint: str = MESHY_BASE) -> dict:
    """Fetch the current status object for a task on ``endpoint``."""
    _check_endpoint(endpoint)
    if not _TASK_ID_RE.match(task_id or ""):
        raise MeshyError(f"invalid Meshy task id: {task_id!r}")
    try:
        resp = requests.get(f"{endpoint}/{task_id}", headers=_headers(),
                            timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise MeshyError(f"Meshy status request failed: {exc}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise MeshyError(f"Meshy returned non-JSON: {resp.text[:200]}") from exc
    if not isinstance(data, dict):
        raise MeshyError(f"Meshy returned unexpected JSON shape: {resp.text[:200]}")
    return data


def wait_for_task(task_id: str, poll_interval: int = 15, timeout: int = 600,
                  stage: str = "task",
                  on_progress: Optional[ProgressFn] = None,
                  endpoint: str = MESHY_BASE) -> dict:
    """Poll until the task SUCCEEDED, raising MeshyError on FAILED/timeout.

    A single transient network blip on one poll must not throw away a billed
    generation, so up to ``_MAX_POLL_FAILURES`` consecutive status-fetch errors
    are tolerated (with a backoff sleep) before giving up. A genuine task
    failure is a status *state*, not an exception, so it is never swallowed.
    """
    start = time.monotonic()
    consecutive_failures = 0
    while time.monotonic() - start < timeout:
        try:
            status = get_task(task_id, endpoint=endpoint)
            consecutive_failures = 0
        except MeshyError as exc:
            consecutive_failures += 1
            if consecutive_failures > _MAX_POLL_FAILURES:
                raise MeshyError(
                    f"Meshy {stage} task {task_id}: {_MAX_POLL_FAILURES} "
                    f"consecutive poll failures; last error: {exc}") from exc
            time.sleep(poll_interval)
            continue
        state = status.get("status", "UNKNOWN")
        if on_progress:
            on_progress(stage, state, int(status.get("progress", 0) or 0))
        if state == "SUCCEEDED":
            return status
        if state == "FAILED":
            raise MeshyError(
                f"Meshy {stage} task {task_id} failed: "
                f"{status.get('message', 'unknown')}"
            )
        time.sleep(poll_interval)
    raise MeshyError(f"Meshy {stage} task {task_id} timed out after {timeout}s")


def download_glb(status: dict, output_path: str) -> str:
    """Stream the GLB for a SUCCEEDED status object to ``output_path``.

    Streamed (not buffered) and capped: the file is written chunk-by-chunk and
    aborted the moment it exceeds ``_MAX_GLB_BYTES``, so a runaway response can
    never exhaust memory.
    """
    glb_url = (status.get("model_urls") or {}).get("glb")
    if not glb_url:
        raise MeshyError("status has no model_urls.glb (task not finished?)")
    if not glb_url.lower().startswith("https://"):
        raise MeshyError(f"refusing non-HTTPS model URL: {glb_url[:80]}")
    output_path = os.path.abspath(output_path)
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    # Download to a sibling temp file and atomically swap it into place only on
    # full success — a failed/oversized/empty download must never delete or
    # corrupt a file the caller already had at output_path.
    fd, tmp_path = tempfile.mkstemp(suffix=".glb.part", dir=out_dir)
    os.close(fd)
    total = 0
    try:
        with requests.get(glb_url, timeout=_DOWNLOAD_TIMEOUT, stream=True) as resp:
            if not resp.url.lower().startswith("https://"):
                raise MeshyError(f"download redirected to non-HTTPS: {resp.url[:80]}")
            resp.raise_for_status()
            with open(tmp_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_GLB_BYTES:
                        raise MeshyError(
                            f"GLB exceeds the {_MAX_GLB_BYTES}-byte cap")
                    fh.write(chunk)
        if total == 0:
            raise MeshyError("downloaded GLB is empty")
        os.replace(tmp_path, output_path)
    except requests.RequestException as exc:
        raise MeshyError(f"GLB download failed: {exc}") from exc
    except OSError as exc:
        raise MeshyError(f"GLB write failed: {exc}") from exc
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass  # best-effort cleanup; never mask the real error
    return output_path


def generate(prompt: str, output_path: str, art_style: str = "realistic",
             should_remesh: bool = True, texture_prompt: Optional[str] = None,
             enable_pbr: bool = True, poll_interval: int = 15,
             timeout: int = 600,
             on_progress: Optional[ProgressFn] = None) -> dict:
    """Full two-stage generation: preview (base mesh) → refine (texturing) →
    download GLB. The refine stage textures the model; ``enable_pbr`` and
    ``texture_prompt`` control it.

    ``timeout`` applies to each polling stage independently. Returns metadata
    including both task ids and the local ``glb_path``.
    """
    preview_id = create_preview_task(prompt, art_style=art_style,
                                     should_remesh=should_remesh)
    wait_for_task(preview_id, poll_interval=poll_interval, timeout=timeout,
                  stage="preview", on_progress=on_progress)

    refine_id = create_refine_task(preview_id, texture_prompt=texture_prompt,
                                   enable_pbr=enable_pbr)
    refine_status = wait_for_task(refine_id, poll_interval=poll_interval,
                                  timeout=timeout, stage="refine",
                                  on_progress=on_progress)

    try:
        glb_path = download_glb(refine_status, output_path)
    except MeshyError as exc:
        # The billed generation already succeeded; surface the refine task id
        # so the caller can re-fetch the model instead of paying again.
        raise MeshyError(
            f"{exc} (refine task {refine_id} succeeded — GLB retrievable by id)"
        ) from exc
    return {
        "preview_task_id": preview_id,
        "refine_task_id": refine_id,
        "glb_path": glb_path,
        "model_urls": refine_status.get("model_urls", {}),
        "art_style": art_style,
        "prompt": prompt,
        "texture_prompt": texture_prompt,
        "enable_pbr": enable_pbr,
    }


# --- image inputs ---------------------------------------------------------

_IMAGE_MIME_FALLBACK = "image/png"
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB cap on a base64-inlined local image


def _looks_like_image(head: bytes) -> bool:
    """True if the leading bytes match a common image format — a guardrail so a
    local non-image file (e.g. a secret) can't be base64-inlined and uploaded."""
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")           # PNG
        or head.startswith(b"\xff\xd8\xff")             # JPEG
        or head[:6] in (b"GIF87a", b"GIF89a")           # GIF
        or head.startswith(b"BM")                       # BMP
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")  # WEBP
    )


def to_image_source(image: str) -> str:
    """Normalize an image reference for Meshy: pass http(s) URLs and existing
    data URIs through unchanged; read a LOCAL file path and return it as a
    base64 ``data:`` URI (Meshy accepts a public URL or a data URI)."""
    if image.startswith(("http://", "https://")):
        return image
    if image.startswith("data:"):
        # base64 inflates ~1.37x; cap generously so a data URI can't be unbounded.
        if len(image) > _MAX_IMAGE_BYTES * 2:
            raise MeshyError("data URI exceeds the inline image cap")
        return image
    path = os.path.abspath(image)
    if not os.path.isfile(path):
        raise MeshyError(f"image is neither a URL nor an existing file: {image}")
    size = os.path.getsize(path)
    if size > _MAX_IMAGE_BYTES:
        raise MeshyError(
            f"image is {size} bytes, over the {_MAX_IMAGE_BYTES}-byte inline cap")
    with open(path, "rb") as fh:
        data = fh.read()
    if not _looks_like_image(data[:16]):
        raise MeshyError(f"file does not look like an image (bad magic): {image}")
    mime = mimetypes.guess_type(path)[0] or _IMAGE_MIME_FALLBACK
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def generate_from_image(image: str, output_path: str, *,
                        enable_pbr: bool = True, should_texture: bool = True,
                        should_remesh: bool = True,
                        texture_prompt: Optional[str] = None,
                        poll_interval: int = 15, timeout: int = 600,
                        on_progress: Optional[ProgressFn] = None) -> dict:
    """Image-to-3D: a photo/render (URL or local path) → textured GLB."""
    payload = {
        "image_url": to_image_source(image),
        "enable_pbr": bool(enable_pbr),
        "should_texture": bool(should_texture),
        "should_remesh": bool(should_remesh),
    }
    if texture_prompt:
        payload["texture_prompt"] = texture_prompt
    task_id = _create(payload, endpoint=EP_IMAGE_TO_3D)
    status = wait_for_task(task_id, poll_interval=poll_interval, timeout=timeout,
                           stage="image-to-3d", endpoint=EP_IMAGE_TO_3D,
                           on_progress=on_progress)
    glb_path = download_glb(status, output_path)
    return {"task_id": task_id, "glb_path": glb_path,
            "model_urls": status.get("model_urls", {}), "source": "image-to-3d"}


def generate_from_images(images: list, output_path: str, *,
                         enable_pbr: bool = True, should_texture: bool = True,
                         should_remesh: bool = True,
                         texture_prompt: Optional[str] = None,
                         poll_interval: int = 15, timeout: int = 600,
                         on_progress: Optional[ProgressFn] = None) -> dict:
    """Multi-image-to-3D: 1–4 reference images → higher-fidelity textured GLB."""
    if not images or not 1 <= len(images) <= 4:
        raise MeshyError("provide 1 to 4 images for multi-image-to-3d")
    payload = {
        "image_urls": [to_image_source(i) for i in images],
        "enable_pbr": bool(enable_pbr),
        "should_texture": bool(should_texture),
        "should_remesh": bool(should_remesh),
    }
    if texture_prompt:
        payload["texture_prompt"] = texture_prompt
    task_id = _create(payload, endpoint=EP_MULTI_IMAGE_TO_3D)
    status = wait_for_task(task_id, poll_interval=poll_interval, timeout=timeout,
                           stage="multi-image-to-3d",
                           endpoint=EP_MULTI_IMAGE_TO_3D, on_progress=on_progress)
    glb_path = download_glb(status, output_path)
    return {"task_id": task_id, "glb_path": glb_path,
            "model_urls": status.get("model_urls", {}),
            "source": "multi-image-to-3d", "image_count": len(images)}


# --- retexture ------------------------------------------------------------

def retexture(output_path: str, *, input_task_id: Optional[str] = None,
              model_url: Optional[str] = None,
              text_style_prompt: Optional[str] = None,
              image_style_url: Optional[str] = None, enable_pbr: bool = True,
              ai_model: Optional[str] = None, poll_interval: int = 15,
              timeout: int = 600,
              on_progress: Optional[ProgressFn] = None) -> dict:
    """Apply NEW textures to an existing model -> a re-textured GLB variant.

    Identify the source model by ``input_task_id`` (a prior Meshy task) OR a
    public ``model_url``; describe the new look with ``text_style_prompt`` OR
    ``image_style_url``. (Meshy fetches the model server-side, so a local file
    path is not accepted here — use a task id or a hosted URL.)"""
    if not (input_task_id or model_url):
        raise MeshyError("retexture needs input_task_id or model_url")
    if not (text_style_prompt or image_style_url):
        raise MeshyError("retexture needs text_style_prompt or image_style_url")
    payload: dict = {"enable_pbr": bool(enable_pbr)}
    if input_task_id:
        payload["input_task_id"] = input_task_id
    if model_url:
        payload["model_url"] = model_url
    if text_style_prompt:
        payload["text_style_prompt"] = text_style_prompt
    if image_style_url:
        payload["image_style_url"] = image_style_url
    if ai_model:
        payload["ai_model"] = ai_model
    task_id = _create(payload, endpoint=EP_RETEXTURE)
    status = wait_for_task(task_id, poll_interval=poll_interval, timeout=timeout,
                           stage="retexture", endpoint=EP_RETEXTURE,
                           on_progress=on_progress)
    glb_path = download_glb(status, output_path)
    return {"task_id": task_id, "glb_path": glb_path,
            "model_urls": status.get("model_urls", {}), "source": "retexture"}


# --- rigging + animation (moving characters) ------------------------------

def rig(*, input_task_id: Optional[str] = None, model_url: Optional[str] = None,
        height_meters: float = 1.7, poll_interval: int = 15, timeout: int = 600,
        on_progress: Optional[ProgressFn] = None) -> dict:
    """Auto-rig a humanoid model (a skeleton for animation). Identify the model
    by ``input_task_id`` (a prior Meshy generation) or a public ``model_url``
    (GLB, ≤300k faces). Returns ``rig_task_id`` (feed it to ``animate``)."""
    if not (input_task_id or model_url):
        raise MeshyError("rig needs input_task_id or model_url")
    if height_meters <= 0:
        raise MeshyError("height_meters must be > 0")
    payload: dict = {"height_meters": float(height_meters)}
    if input_task_id:
        payload["input_task_id"] = input_task_id
    if model_url:
        payload["model_url"] = model_url
    task_id = _create(payload, endpoint=EP_RIGGING)
    status = wait_for_task(task_id, poll_interval=poll_interval, timeout=timeout,
                           stage="rigging", endpoint=EP_RIGGING,
                           on_progress=on_progress)
    return {"rig_task_id": task_id, "model_urls": status.get("model_urls", {}),
            "source": "rigging"}


def animate(rig_task_id: str, action_id: int, output_path: str, *,
            fps: Optional[int] = None, poll_interval: int = 15,
            timeout: int = 600,
            on_progress: Optional[ProgressFn] = None) -> dict:
    """Apply an animation (``action_id`` from Meshy's library — e.g. 0=Idle,
    1=Walking, 4=Attack, 22=Dancing) to a rigged model, and download the
    animated GLB (skeletal animation baked in for rendering)."""
    if not _TASK_ID_RE.match(rig_task_id or ""):
        raise MeshyError(f"invalid rig_task_id: {rig_task_id!r}")
    if not isinstance(action_id, int) or action_id < 0:
        raise MeshyError(f"action_id must be a non-negative integer, got {action_id!r}")
    payload: dict = {"rig_task_id": rig_task_id, "action_id": int(action_id)}
    if fps in (24, 25, 30, 60):
        payload["post_process"] = {"operation_type": "change_fps", "fps": fps}
    task_id = _create(payload, endpoint=EP_ANIMATION)
    status = wait_for_task(task_id, poll_interval=poll_interval, timeout=timeout,
                           stage="animation", endpoint=EP_ANIMATION,
                           on_progress=on_progress)
    glb_path = download_glb(status, output_path)
    return {"task_id": task_id, "glb_path": glb_path,
            "model_urls": status.get("model_urls", {}),
            "action_id": action_id, "source": "animation"}
