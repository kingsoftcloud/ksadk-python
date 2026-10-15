"""Host workspace observation adapters for the Native Harness."""

from .native_directory import DirectoryWorkspacePort, LocalWorkspacePort
from .native_git import GitWorkspacePort

__all__ = ["DirectoryWorkspacePort", "GitWorkspacePort", "LocalWorkspacePort"]
