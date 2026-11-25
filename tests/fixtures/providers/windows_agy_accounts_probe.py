"""Explicit Windows CI evidence for private AGY profile credential inheritance."""
from pathlib import Path
import ctypes as C
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.platform.artifact_windows import _native
from tools.providers.agy_accounts import AgyAccounts


def main():
    if sys.platform != 'win32':
        raise SystemExit('This probe requires real Windows NTFS')
    with tempfile.TemporaryDirectory(prefix='fleet-agy-private-') as folder:
        accounts = AgyAccounts(Path(folder)/'accounts')
        profile = accounts.profile('pa_'+'a'*32)
        token = profile/'fixture-token.json'
        token.write_text('private', encoding='utf-8')
        native = _native()
        handle = native.kernel.CreateFileW(str(token), 0x20000 | 0x80, 3, None, 3, 0x00200000, None)
        assert handle not in {None, C.c_void_p(-1).value}
        try:
            native.validate_acl(handle)
            owner, fingerprint = native.security_identity(handle)
            assert owner and len(fingerprint)==64
            # artifact_windows rejects broad writers. Credential confidentiality
            # also requires proving every allow ACE names a trusted reader.
            from tools.platform.artifact_windows import HANDLE, _ACLSize, _ACE
            sid_owner, acl, descriptor = HANDLE(), HANDLE(), HANDLE()
            result = native.security.GetSecurityInfo(handle,1,5,C.byref(sid_owner),None,C.byref(acl),None,C.byref(descriptor))
            assert result==0 and acl.value
            try:
                size = _ACLSize()
                native.checked(native.security.GetAclInformation(acl,C.byref(size),C.sizeof(size),2))
                trusted = {native.current_user(),'S-1-5-18','S-1-5-32-544'}
                assert 1 <= size.AceCount <= 16
                for index in range(size.AceCount):
                    pointer = HANDLE()
                    native.checked(native.security.GetAce(acl,index,C.byref(pointer)))
                    ace = C.cast(pointer,C.POINTER(_ACE)).contents
                    assert ace.AceType==0
                    assert native.sid(pointer.value + _ACE.SidStart.offset) in trusted
            finally:
                native.kernel.LocalFree(descriptor)
        finally:
            native.close(handle)
    print('AGY inherited private NTFS DACL: passed')


if __name__ == '__main__':
    main()
