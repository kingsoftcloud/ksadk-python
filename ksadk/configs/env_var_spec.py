"""Shared environment-variable registry value object."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvVarSpec:
    name: str
    module: str
    purpose: str
    default: str = ""
    sensitive: bool = False
    documented: bool = True


__all__ = ["EnvVarSpec"]
