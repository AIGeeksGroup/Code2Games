"""Shared output isolation for Code2Games gameplay staging."""
import os
import re


DEMO_NAME_ENV = "CODE2GAMES_DEMO_NAME"
_VALID_DEMO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def get_demo_name():
    value = str(os.environ.get(DEMO_NAME_ENV) or "").strip()
    if value and not _VALID_DEMO_NAME.fullmatch(value):
        raise ValueError(
            "%s must contain only letters, digits, underscores, or hyphens "
            "and must not exceed 64 characters" % DEMO_NAME_ENV
        )
    return value


def get_staging_root(project_root):
    root = os.path.abspath(os.fspath(project_root))
    base = os.path.join(root, "output", "game_staging")
    demo_name = get_demo_name()
    return os.path.join(base, demo_name) if demo_name else base


def get_packet_dir(project_root):
    return os.path.join(get_staging_root(project_root), "default_camera_packet")


def get_generated_glb_prefix():
    demo_name = get_demo_name()
    base = "./assets/generated_glb/"
    return base + demo_name + "/" if demo_name else base
