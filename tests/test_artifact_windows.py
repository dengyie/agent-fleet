"""Platform-neutral Windows path policy; native checks run on Windows itself."""
from __future__ import annotations

import os

import pytest

from hub.domain.artifact_receive import ArtifactReceiveError
from tools.platform.artifact_windows import validate_root, validate_component, windows_supported
from tools.platform import artifact_windows as windows


@pytest.mark.parametrize("path", [
    r"C:\Users\mango\inputs", r"D:\fleet-inputs", "C:\\",
])
def test_windows_local_absolute_root_policy(path: str) -> None:
    assert validate_root(path).drive.upper() in {"C:", "D:"}


@pytest.mark.parametrize("path", [
    r"relative\inputs", r"C:inputs", r"\inputs", r"\\server\share\inputs",
    r"\\?\C:\inputs", r"\\.\C:\inputs", r"C:\inputs:stream", r"C:\inputs\..\escape",
    "C:\\inputs\\trailing.", "C:\\inputs\\trailing ", r"C:\NUL\inputs", r"C:\COM1.log",
    "C:\\inputs\\control\x01", r"C:\inputs\wild*", "C:\\" + "a\\" * 129,
])
def test_windows_ambiguous_or_external_roots_are_rejected(path: str) -> None:
    with pytest.raises(ArtifactReceiveError, match="unsafe_input_path"):
        validate_root(path)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "x:stream", "a.", "a ", "NUL", "lpt9.bin", "x\0"])
def test_windows_child_is_exactly_one_unambiguous_component(name: str) -> None:
    with pytest.raises(ArtifactReceiveError, match="unsafe_input_path"):
        validate_component(name)


def test_windows_adapter_does_not_claim_support_on_other_platforms() -> None:
    if os.name != "nt":
        assert windows_supported() is False
    assert validate_component("." + "a" * 64 + ".part") is None


@pytest.mark.parametrize("path", [
    "report.bin", "source/report.tar", "source/完整正文.md", "/".join(["a"] * 32), "a" * 255,
])
def test_windows_relative_output_policy(path: str) -> None:
    assert windows.validate_relative_path(path) == tuple(path.split("/"))


@pytest.mark.parametrize("path", [
    "", ".", "..", "../source", "a/../source", "a/./source", "a//source", "a/", "/source",
    r"a\source", r"C:\source", "C:source", "//server/share", "report.bin:secret", "a/NUL",
    "a/com1.log", "a/lpt².bin", "a/report.", "a/report ", "a/control\0", "a/wild*",
    "a\ud800", "/".join(["a"] * 33), "a" * 256, "a" * 255 + "/" + "b" * 255 + "/c",
])
def test_windows_relative_output_rejects_ambiguous_components(path: str) -> None:
    with pytest.raises(ArtifactReceiveError, match="unsafe_input_path"):
        windows.validate_relative_path(path)
