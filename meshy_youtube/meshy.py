"""Meshy.ai text-to-3D generation (two-stage preview → refine).

Meshy's text-to-3D API is two tasks: a ``preview`` task turns a prompt into a
base mesh, then a ``refine`` task (referencing the preview's id) textures it.
``generate`` runs both, polls each to completion, and downloads the final GLB.

This is the API-correct flow and works on any Meshy plan. (BoTTube historically
issued a single bare ``mode="refine"`` call; that path is not reproduced here
because refine requires a completed ``preview_task_id``.)
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from typing import Callable, Optional

import requests

MESHY_BASE = "https://api.meshy.ai/openapi/v2/text-to-3d"

# Meshy task ids are uuid-like; this allowlist stops a crafted id from doing
# path traversal or query injection into the authenticated status URL.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

ART_STYLES = ("realistic", "cartoon", "low-poly", "sculpture")

# Network timeouts (seconds). Generation progress is handled by polling, not by
# a single long HTTP call, so these stay short and fail fast on a dead network.
_HTTP_TIMEOUT = 60
_DOWNLOAD_TIMEOUT = 300
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


def _create(payload: dict) -> str:
    try:
        resp = requests.post(MESHY_BASE, headers=_headers(), json=payload,
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


def create_refine_task(preview_task_id: str) -> str:
    """Create a refine (texturing) task for a completed preview task."""
    if not preview_task_id:
        raise MeshyError("preview_task_id is required to refine")
    return _create({"mode": "refine", "preview_task_id": preview_task_id})


def get_task(task_id: str) -> dict:
    """Fetch the current status object for a task."""
    if not _TASK_ID_RE.match(task_id or ""):
        raise MeshyError(f"invalid Meshy task id: {task_id!r}")
    try:
        resp = requests.get(f"{MESHY_BASE}/{task_id}", headers=_headers(),
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
                  on_progress: Optional[ProgressFn] = None) -> dict:
    """Poll until the task SUCCEEDED, raising MeshyError on FAILED/timeout."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        status = get_task(task_id)
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
             should_remesh: bool = True, poll_interval: int = 15,
             timeout: int = 600,
             on_progress: Optional[ProgressFn] = None) -> dict:
    """Full two-stage generation: preview → refine → download GLB.

    ``timeout`` applies to each polling stage independently. Returns metadata
    including both task ids and the local ``glb_path``.
    """
    preview_id = create_preview_task(prompt, art_style=art_style,
                                     should_remesh=should_remesh)
    wait_for_task(preview_id, poll_interval=poll_interval, timeout=timeout,
                  stage="preview", on_progress=on_progress)

    refine_id = create_refine_task(preview_id)
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
    }
