"""Build and manage thin overlay Docker images for SWE-bench task instances."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
IMAGES_JSON = REPO_ROOT / "suites" / "verified-mvp-v1.images.json"


def get_base_image_for_instance(instance_id: str) -> str:
    """Lookup the pre-pulled SWE-bench base image for an instance_id."""
    if IMAGES_JSON.exists():
        try:
            data = json.loads(IMAGES_JSON.read_text())
            entry = data.get("images", {}).get(instance_id)
            if entry and "image" in entry:
                return entry["image"]
        except Exception as e:
            logger.warning("Could not read %s: %s", IMAGES_JSON, e)

    # Fallback to standard convention
    key = instance_id.replace("__", "_1776_")
    return f"swebench/sweb.eval.x86_64.{key}:latest"


def overlay_tag_for_instance(instance_id: str) -> str:
    """Overlay image tag for an instance."""
    clean_id = instance_id.lower().replace("__", "-").replace("_", "-")
    return f"subbench-agy:{clean_id}"


def image_exists(tag: str) -> bool:
    """Check if a docker image tag exists locally."""
    res = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        check=False,
    )
    return res.returncode == 0


def build_overlay_image(
    instance_id: str,
    base_image: str | None = None,
    dockerfile: Path | None = None,
) -> str:
    """Build the thin agy overlay on top of the SWE-bench instance image if not present."""
    tag = overlay_tag_for_instance(instance_id)
    if image_exists(tag):
        logger.info("Overlay image %s already exists", tag)
        return tag

    base = base_image or get_base_image_for_instance(instance_id)
    df = dockerfile or (REPO_ROOT / "docker" / "Dockerfile.agy-overlay")

    logger.info("Building overlay %s on base %s", tag, base)
    cmd = [
        "docker",
        "build",
        "-t",
        tag,
        "--build-arg",
        f"BASE_IMAGE={base}",
        "-f",
        str(df),
        str(REPO_ROOT),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(
            f"Failed to build overlay image {tag}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
        )
    return tag


def ensure_probe_image(
    image_tag: str = "subbench-agy:probe",
    sample_instance_id: str = "pytest-dev__pytest-5809",
) -> str:
    """Ensure a thin probe image exists locally without pulling new base layers."""
    if image_exists(image_tag):
        return image_tag
    base = get_base_image_for_instance(sample_instance_id)
    df = REPO_ROOT / "docker" / "Dockerfile.agy-overlay"
    logger.info("Building probe overlay %s on base %s", image_tag, base)
    cmd = [
        "docker",
        "build",
        "-t",
        image_tag,
        "--build-arg",
        f"BASE_IMAGE={base}",
        "-f",
        str(df),
        str(REPO_ROOT),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(
            f"Failed to build probe image {image_tag}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
        )
    return image_tag
