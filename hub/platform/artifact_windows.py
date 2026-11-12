"""Windows artifact I/O with pinned NT parents and no pathname fallback."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
import ctypes as C
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
from typing import BinaryIO

from hub.domain.artifact_receive import ArtifactReceiveError

U8, U16, U32, U64 = C.c_uint8, C.c_uint16, C.c_uint32, C.c_uint64
HANDLE = C.c_void_p
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
_READ, _WRITE, _DELETE, _SYNC, _CONTROL = 0x80000000, 0x40000000, 0x10000, 0x100000, 0x20000
_ATTR_REPARSE = 0x400


def validate_component(name: str) -> None:
    if (type(name) is not str or not name or name in {".", ".."} or name[-1] in {".", " "}
            or any(ord(char) < 32 or char in '\\/:*?"<>|' for char in name) or _RESERVED.match(name)):
        raise ArtifactReceiveError("unsafe_input_path")
    try:
        size = len(name.encode("utf-16-le"))
    except UnicodeError as exc:
        raise ArtifactReceiveError("unsafe_input_path") from exc
    if size > 510:
        raise ArtifactReceiveError("unsafe_input_path")


def validate_root(value: str) -> PureWindowsPath:
    if (type(value) is not str or len(value) < 3 or not value[0].isascii() or not value[0].isalpha()
            or value[1:3] != ":\\" or "/" in value or len(value) > 32760):
        raise ArtifactReceiveError("unsafe_input_path")
    parts = value[3:].split("\\") if len(value) > 3 else []
    if len(parts) > 128:
        raise ArtifactReceiveError("unsafe_input_path")
    for part in parts:
        validate_component(part)
    return PureWindowsPath(value)


def validate_relative_path(value: str) -> tuple[str, ...]:
    if type(value) is not str or not value:
        raise ArtifactReceiveError("unsafe_input_path")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ArtifactReceiveError("unsafe_input_path") from exc
    parts = tuple(value.split("/"))
    if size > 512 or len(parts) > 32:
        raise ArtifactReceiveError("unsafe_input_path")
    for part in parts:
        validate_component(part)
    return parts


class _Unicode(C.Structure):
    _fields_ = [("Length", U16), ("MaximumLength", U16), ("Buffer", HANDLE)]


class _Attributes(C.Structure):
    _fields_ = [("Length", U32), ("RootDirectory", HANDLE), ("ObjectName", C.POINTER(_Unicode)),
                ("Attributes", U32), ("SecurityDescriptor", HANDLE), ("SecurityQualityOfService", HANDLE)]


class _Status(C.Structure):
    _fields_ = [("Status", HANDLE), ("Information", C.c_size_t)]


class _Basic(C.Structure):
    _fields_ = [("CreationTime", C.c_int64), ("LastAccessTime", C.c_int64),
                ("LastWriteTime", C.c_int64), ("ChangeTime", C.c_int64), ("FileAttributes", U32)]


class _Standard(C.Structure):
    _fields_ = [("AllocationSize", C.c_int64), ("EndOfFile", C.c_int64), ("NumberOfLinks", U32),
                ("DeletePending", U8), ("Directory", U8)]


class _FileID(C.Structure):
    _fields_ = [("VolumeSerialNumber", U64), ("FileId", U8 * 16)]


class _ACLSize(C.Structure):
    _fields_ = [("AceCount", U32), ("AclBytesInUse", U32), ("AclBytesFree", U32)]


class _ACE(C.Structure):
    _fields_ = [("AceType", U8), ("AceFlags", U8), ("AceSize", U16), ("Mask", U32), ("SidStart", U32)]


class _Overlapped(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                ("Offset", U32), ("OffsetHigh", U32), ("hEvent", HANDLE)]


class _Rename(C.Structure):
    _fields_ = [("ReplaceIfExists", U8), ("RootDirectory", HANDLE), ("FileNameLength", U32), ("FileName", U16 * 1)]


@dataclass(frozen=True, slots=True)
class FileInfo:
    st_dev: int
    st_ino: int
    st_size: int
    st_nlink: int
    directory: bool
    attributes: int


class _Native:
    def __init__(self) -> None:
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.nt = C.WinDLL("ntdll", use_last_error=True)
        self.security = C.WinDLL("advapi32", use_last_error=True)
        self._bind(self.kernel, "CreateFileW", HANDLE, [C.c_wchar_p, U32, U32, HANDLE, U32, U32, HANDLE])
        self._bind(self.kernel, "CloseHandle", U32, [HANDLE])
        self._bind(self.kernel, "GetFileInformationByHandleEx", U32, [HANDLE, U32, HANDLE, U32])
        self._bind(self.kernel, "GetFileType", U32, [HANDLE])
        self._bind(self.kernel, "GetDriveTypeW", U32, [C.c_wchar_p])
        self._bind(self.kernel, "GetVolumeInformationByHandleW", U32,
            [HANDLE, C.c_wchar_p, U32, C.POINTER(U32), C.POINTER(U32), C.POINTER(U32), C.c_wchar_p, U32])
        self._bind(self.kernel, "FlushFileBuffers", U32, [HANDLE])
        self._bind(self.kernel, "GetCurrentProcess", HANDLE, [])
        self._bind(self.kernel, "LocalFree", HANDLE, [HANDLE])
        self._bind(self.kernel, "DuplicateHandle", U32, [HANDLE, HANDLE, HANDLE, C.POINTER(HANDLE), U32, U32, U32])
        self._bind(self.kernel, "LockFileEx", U32, [HANDLE, U32, U32, U32, U32, C.POINTER(_Overlapped)])
        self._bind(self.nt, "NtCreateFile", C.c_int32, [C.POINTER(HANDLE), U32, C.POINTER(_Attributes),
            C.POINTER(_Status), HANDLE, U32, U32, U32, U32, HANDLE, U32])
        self._bind(self.nt, "NtSetInformationFile", C.c_int32, [HANDLE, C.POINTER(_Status), HANDLE, U32, U32])
        self._bind(self.nt, "RtlNtStatusToDosError", U32, [C.c_int32])
        self._bind(self.security, "OpenProcessToken", U32, [HANDLE, U32, C.POINTER(HANDLE)])
        self._bind(self.security, "GetTokenInformation", U32, [HANDLE, U32, HANDLE, U32, C.POINTER(U32)])
        self._bind(self.security, "GetSecurityInfo", U32, [HANDLE, U32, U32, C.POINTER(HANDLE),
            HANDLE, C.POINTER(HANDLE), HANDLE, C.POINTER(HANDLE)])
        self._bind(self.security, "ConvertSidToStringSidW", U32, [HANDLE, C.POINTER(HANDLE)])
        self._bind(self.security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", U32,
            [C.c_wchar_p, U32, C.POINTER(HANDLE), C.POINTER(U32)])
        self._bind(self.security, "GetAclInformation", U32, [HANDLE, HANDLE, U32, U32])
        self._bind(self.security, "GetAce", U32, [HANDLE, U32, C.POINTER(HANDLE)])
        self._bind(self.security, "GetSecurityDescriptorControl", U32, [HANDLE, C.POINTER(U16), C.POINTER(U32)])

    @staticmethod
    def _bind(library: object, name: str, result: object, args: list[object]) -> None:
        function = getattr(library, name)
        function.restype, function.argtypes = result, args

    @staticmethod
    def checked(result: int) -> None:
        if not result:
            raise C.WinError(C.get_last_error())

    def status(self, value: int) -> None:
        if value < 0:
            raise C.WinError(self.nt.RtlNtStatusToDosError(value))

    def close(self, handle: int) -> None:
        self.checked(self.kernel.CloseHandle(handle))

    def sid(self, pointer: int) -> str:
        text = HANDLE()
        self.checked(self.security.ConvertSidToStringSidW(pointer, C.byref(text)))
        try:
            return C.wstring_at(text.value)
        finally:
            self.kernel.LocalFree(text)

    def current_user(self) -> str:
        token, needed = HANDLE(), U32()
        self.checked(self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, C.byref(token)))
        try:
            self.security.GetTokenInformation(token, 1, None, 0, C.byref(needed))
            if not 0 < needed.value <= 65536:
                raise ArtifactReceiveError("unsafe_input_path")
            data = C.create_string_buffer(needed.value)
            self.checked(self.security.GetTokenInformation(token, 1, data, needed, C.byref(needed)))
            return self.sid(C.cast(data, C.POINTER(HANDLE))[0])
        finally:
            self.close(token.value)

    @contextmanager
    def private_security(self) -> Iterator[int]:
        user = self.current_user()
        descriptor, size = HANDLE(), U32()
        sddl = f"O:{user}D:P(A;;FA;;;{user})(A;;FA;;;SY)(A;;FA;;;BA)"
        self.checked(self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, C.byref(descriptor), C.byref(size)))
        try:
            yield descriptor.value
        finally:
            self.kernel.LocalFree(descriptor)

    def validate_acl(self, handle: int) -> None:
        self.security_identity(handle)

    def security_identity(self, handle: int) -> tuple[str, str]:
        owner, acl, descriptor = HANDLE(), HANDLE(), HANDLE()
        error = self.security.GetSecurityInfo(handle, 1, 5, C.byref(owner), None, C.byref(acl), None, C.byref(descriptor))
        if error:
            raise C.WinError(error)
        try:
            user = self.current_user()
            if not owner.value or self.sid(owner.value) != user or not acl.value:
                raise ArtifactReceiveError("unsafe_input_path")
            size = _ACLSize()
            self.checked(self.security.GetAclInformation(acl, C.byref(size), C.sizeof(size), 2))
            if size.AceCount > 256:
                raise ArtifactReceiveError("unsafe_input_path")
            trusted = {user, "S-1-5-18", "S-1-5-32-544"}
            entries = [self._validate_ace(acl.value, index, trusted) for index in range(size.AceCount)]
            control, revision = U16(), U32()
            self.checked(self.security.GetSecurityDescriptorControl(descriptor, C.byref(control), C.byref(revision)))
            acl_revision = C.cast(acl, C.POINTER(U8)).contents.value
            encoded = json.dumps([control.value, revision.value, acl_revision, user, entries],
                separators=(",", ":"), ensure_ascii=True).encode("ascii")
            return user, hashlib.sha256(encoded).hexdigest()
        finally:
            self.kernel.LocalFree(descriptor)

    def _validate_ace(self, acl: int, index: int, trusted: set[str]) -> tuple[int, int, int, str]:
        pointer = HANDLE()
        self.checked(self.security.GetAce(acl, index, C.byref(pointer)))
        ace = C.cast(pointer, C.POINTER(_ACE)).contents
        if ace.AceType not in {0, 1} or ace.AceSize < 12:
            raise ArtifactReceiveError("unsafe_input_path")
        dangerous = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000 | 0x02000000
        sid = self.sid(pointer.value + _ACE.SidStart.offset)
        if ace.AceType == 0 and ace.Mask & dangerous and sid not in trusted:
            raise ArtifactReceiveError("unsafe_input_path")
        return ace.AceType, ace.AceFlags, ace.Mask, sid

    def info(self, handle: int) -> FileInfo:
        if self.kernel.GetFileType(handle) != 1:
            raise ArtifactReceiveError("unsafe_input_path")
        basic, standard, identity = _Basic(), _Standard(), _FileID()
        for kind, value in ((0, basic), (1, standard), (18, identity)):
            self.checked(self.kernel.GetFileInformationByHandleEx(handle, kind, C.byref(value), C.sizeof(value)))
        if basic.FileAttributes & _ATTR_REPARSE or standard.DeletePending:
            raise ArtifactReceiveError("unsafe_input_path")
        return FileInfo(identity.VolumeSerialNumber, int.from_bytes(identity.FileId, "little"),
            standard.EndOfFile, standard.NumberOfLinks, bool(standard.Directory), basic.FileAttributes)

    def validate_volume(self, handle: int) -> None:
        serial, maximum, flags = U32(), U32(), U32()
        filesystem = C.create_unicode_buffer(32)
        self.checked(self.kernel.GetVolumeInformationByHandleW(handle, None, 0, C.byref(serial),
            C.byref(maximum), C.byref(flags), filesystem, len(filesystem)))
        if filesystem.value != "NTFS" or not flags.value & 8:
            raise ArtifactReceiveError("unsafe_input_path")

    def child(self, parent: int, name: str, *, access: int, directory: bool = False,
              create: bool = False, exclusive: bool = False, share_write: bool = True) -> int:
        validate_component(name)
        data = C.create_unicode_buffer(name)
        encoded = name.encode("utf-16-le")
        text = _Unicode(len(encoded), len(encoded) + 2, C.cast(data, HANDLE))
        attributes = _Attributes(C.sizeof(_Attributes), parent, C.pointer(text), 0x1040, None, None)
        options = 0x20 | 0x200000 | 2 | (1 if directory else 0x40)
        handle, status = HANDLE(), _Status()
        with self.private_security() if create else nullcontext(None) as security:
            attributes.SecurityDescriptor = security if create else None
            self.status(self.nt.NtCreateFile(C.byref(handle), access | _SYNC | _CONTROL, C.byref(attributes),
                C.byref(status), None, 0x80, 3 if share_write else 1,
                2 if exclusive else 3 if create else 1, options, None, 0))
        try:
            info = self.info(handle.value)
            if info.directory != directory or (not directory and info.st_nlink != 1):
                raise ArtifactReceiveError("unsafe_input_path")
            if not directory:
                self.validate_acl(handle.value)
            return handle.value
        except BaseException:
            self.close(handle.value)
            raise


@lru_cache(maxsize=1)
def _native() -> _Native:
    return _Native()


def windows_supported() -> bool:
    if os.name != "nt":
        return False
    try:
        _native()
    except (OSError, AttributeError):
        return False
    return True


def _handle(descriptor: int) -> int:
    import msvcrt
    return msvcrt.get_osfhandle(descriptor)


def _descriptor(handle: int, *, writable: bool) -> int:
    import msvcrt
    try:
        return msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if writable else os.O_RDONLY))
    except BaseException:
        _native().close(handle)
        raise


@contextmanager
def root_handle(root: Path, *, private_child: str | None = None) -> Iterator[int]:
    path = validate_root(str(root))
    if not windows_supported():
        raise ArtifactReceiveError("capability_unavailable")
    native = _native()
    try:
        with ExitStack() as stack:
            if native.kernel.GetDriveTypeW(path.anchor) != 3:
                raise ArtifactReceiveError("unsafe_input_path")
            handle = native.kernel.CreateFileW(path.anchor, _CONTROL | _SYNC | 0x81, 3, None, 3, 0x02200000, None)
            if handle in {None, C.c_void_p(-1).value}:
                raise C.WinError(C.get_last_error())
            stack.callback(native.close, handle)
            if not native.info(handle).directory:
                raise ArtifactReceiveError("unsafe_input_path")
            native.validate_volume(handle)
            for part in path.parts[1:]:
                handle = native.child(handle, part, access=0x81, directory=True)
                stack.callback(native.close, handle)
            if private_child is not None:
                # Keep application ACLs intact; slots use a private pinned child.
                handle = native.child(handle, private_child, access=0x87, directory=True, create=True)
                stack.callback(native.close, handle)
            native.validate_acl(handle)
            yield _borrowed_directory(handle, stack)
    except OSError as exc:
        raise ArtifactReceiveError("unsafe_input_path") from exc


def _borrowed_directory(handle: int, stack: ExitStack) -> int:
    """The duplicate descriptor owns its handle; ancestors stay pinned too."""
    native = _native()
    duplicate = HANDLE()
    process = native.kernel.GetCurrentProcess()
    native.checked(native.kernel.DuplicateHandle(process, handle, process, C.byref(duplicate), 0, 0, 2))
    descriptor = _descriptor(duplicate.value, writable=False)
    stack.callback(os.close, descriptor)
    return descriptor


def file_info(descriptor: int) -> FileInfo:
    return _native().info(_handle(descriptor))


def source_identity(descriptor: int) -> dict[str, str]:
    native, handle = _native(), _handle(descriptor)
    info, basic = native.info(handle), _Basic()
    if info.directory or info.st_nlink != 1:
        raise ArtifactReceiveError("unsafe_input_path")
    native.checked(native.kernel.GetFileInformationByHandleEx(handle, 0, C.byref(basic), C.sizeof(basic)))
    owner, acl = native.security_identity(handle)
    return {"dev": str(info.st_dev), "ino": str(info.st_ino), "size": str(info.st_size),
        "mtime_ns": str(basic.LastWriteTime * 100), "ctime_ns": str(basic.ChangeTime * 100),
        "mode": str(info.attributes), "uid": owner, "nlink": str(info.st_nlink), "acl": acl}


@contextmanager
def source_handle(root: Path, relative_path: str) -> Iterator[tuple[BinaryIO, dict[str, str]]]:
    parts = validate_relative_path(relative_path)
    try:
        with root_handle(root) as root_fd, ExitStack() as handles:
            native, directory = _native(), _handle(root_fd)
            root_info = file_info(root_fd)
            for part in parts[:-1]:
                directory = native.child(directory, part, access=0x81, directory=True)
                handles.callback(native.close, directory)
                native.validate_acl(directory)
            descriptor = _descriptor(native.child(directory, parts[-1], access=_READ, share_write=False), writable=False)
            handles.callback(os.close, descriptor)
            source = handles.enter_context(open(descriptor, "rb", closefd=False))
            identity = source_identity(descriptor) | {"root_dev": str(root_info.st_dev), "root_ino": str(root_info.st_ino)}
            yield source, identity
    except OSError as exc:
        raise ArtifactReceiveError("unsafe_input_path") from exc


def open_child(parent: int, name: str, *, writable: bool, create: bool = False, exclusive: bool = False) -> int:
    access = _READ | (_WRITE if writable else 0)
    handle = _native().child(_handle(parent), name, access=access, create=create, exclusive=exclusive)
    return _descriptor(handle, writable=writable)


def entry(parent: int, name: str) -> FileInfo | None:
    try:
        descriptor = open_child(parent, name, writable=False)
    except FileNotFoundError:
        return None
    try:
        return file_info(descriptor)
    finally:
        os.close(descriptor)


def publish(parent: int, source: str, target: str) -> None:
    validate_component(target)
    native = _native()
    handle = native.child(_handle(parent), source, access=_READ | _WRITE | _DELETE)
    try:
        native.checked(native.kernel.FlushFileBuffers(handle))
        encoded = target.encode("utf-16-le")
        size = _Rename.FileName.offset + len(encoded)
        data = C.create_string_buffer(max(size, C.sizeof(_Rename)))
        rename = C.cast(data, C.POINTER(_Rename)).contents
        rename.ReplaceIfExists, rename.RootDirectory, rename.FileNameLength = 0, _handle(parent), len(encoded)
        C.memmove(C.addressof(data) + _Rename.FileName.offset, encoded, len(encoded))
        status = _Status()
        native.status(native.nt.NtSetInformationFile(handle, C.byref(status), data, size, 10))
        native.checked(native.kernel.FlushFileBuffers(handle))
    finally:
        native.close(handle)


def try_lock(parent: int, name: str) -> int | None:
    descriptor = open_child(parent, name, writable=True, create=True)
    try:
        overlap = _Overlapped()
        native = _native()
        native.checked(native.kernel.LockFileEx(_handle(descriptor), 3, 0, 1, 0, C.byref(overlap)))
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        if exc.winerror == 33:
            return None
        raise
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def lock_file(parent: int, name: str) -> Iterator[None]:
    descriptor = try_lock(parent, name)
    if descriptor is None:
        raise C.WinError(33)
    try:
        yield
    finally:
        os.close(descriptor)
