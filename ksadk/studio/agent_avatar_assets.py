"""Validated, content-addressed assets for Studio Agent avatars."""

from __future__ import annotations

import hashlib
import io
import re
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.workspace import Workspace


class AgentAvatarAssetStore:
    MAX_BYTES = 2 * 1024 * 1024
    MIN_EDGE = 32
    MAX_EDGE = 2048
    _ASSET_NAME = re.compile(r"^(?P<digest>[0-9a-f]{64})\.(?P<extension>png|webp)$")
    _MIME_BY_FORMAT = {"PNG": "image/png", "WEBP": "image/webp"}
    _EXTENSION_BY_FORMAT = {"PNG": "png", "WEBP": "webp"}

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def store(self, content: bytes, *, content_type: str) -> dict:
        if not content:
            raise StudioError(
                "AGENT_AVATAR_EMPTY",
                "头像文件不能为空",
                status_code=422,
                field="file",
            )
        if len(content) > self.MAX_BYTES:
            raise StudioError(
                "AGENT_AVATAR_TOO_LARGE",
                "头像文件不能超过 2 MiB",
                status_code=413,
                field="file",
                details={"maxBytes": self.MAX_BYTES},
            )
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if normalized_type not in self._MIME_BY_FORMAT.values():
            raise StudioError(
                "AGENT_AVATAR_TYPE_UNSUPPORTED",
                "头像仅支持 PNG 或 WebP",
                status_code=415,
                field="file",
            )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    image.load()
                    image_format = str(image.format or "").upper()
                    width, height = image.size
        except (
            UnidentifiedImageError,
            OSError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise StudioError(
                "AGENT_AVATAR_INVALID",
                "头像文件不是有效图片",
                status_code=422,
                field="file",
            ) from exc

        expected_type = self._MIME_BY_FORMAT.get(image_format)
        if expected_type is None or expected_type != normalized_type:
            raise StudioError(
                "AGENT_AVATAR_TYPE_MISMATCH",
                "头像内容与文件类型不一致",
                status_code=422,
                field="file",
            )
        if width != height:
            raise StudioError(
                "AGENT_AVATAR_NOT_SQUARE",
                "头像必须裁剪为 1:1 正方形",
                status_code=422,
                field="file",
                details={"width": width, "height": height},
            )
        if width < self.MIN_EDGE or width > self.MAX_EDGE:
            raise StudioError(
                "AGENT_AVATAR_DIMENSIONS_INVALID",
                "头像边长必须在 32 到 2048 像素之间",
                status_code=422,
                field="file",
                details={"width": width, "height": height},
            )

        digest = hashlib.sha256(content).hexdigest()
        extension = self._EXTENSION_BY_FORMAT[image_format]
        asset_name = f"{digest}.{extension}"
        path = self.workspace.resolve(Path(".agentkit/assets/agent-avatars") / asset_name)
        if not path.is_file():
            self.workspace.atomic_write_bytes(path, content)
        return {
            "digest": digest,
            "url": f"/api/v1/assets/agent-avatars/{asset_name}",
            "mimeType": expected_type,
            "size": len(content),
            "width": width,
            "height": height,
        }

    def get(self, asset_name: str) -> tuple[Path, str]:
        match = self._ASSET_NAME.fullmatch(asset_name)
        if match is None:
            raise not_found("agent-avatar", asset_name)
        extension = match.group("extension")
        path = self.workspace.resolve(Path(".agentkit/assets/agent-avatars") / asset_name)
        if not path.is_file():
            raise not_found("agent-avatar", asset_name)
        return path, "image/png" if extension == "png" else "image/webp"
