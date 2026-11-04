"""Windows contention retries preserve atomicity, the original failure and old data."""

import ctypes
import errno
import json
import os
import threading
from pathlib import Path

import pytest

from ops import report_files as files


def denied(path,winerror=5,*,message='access denied'):
    error=PermissionError(errno.EACCES,message,str(path))
    error.winerror=winerror
    return error


@pytest.mark.parametrize('winerror',[5,32,33])
def test_transient_windows_denial_reuses_one_closed_fsynced_temporary_file(tmp_path,monkeypatch,winerror):
    target=tmp_path/'report.json'
    target.write_text('{"old":true}',encoding='utf-8')
    handles,sleeps,replacements,syncs=[],[],[],[]
    factory,replace,sync=files.tempfile.NamedTemporaryFile,files.os.replace,files.os.fsync
    def temporary(*args,**kwargs):
        handle=factory(*args,**kwargs)
        handles.append(handle)
        return handle
    def synced(fd):
        syncs.append(fd)
        return sync(fd)
    def contend(source,destination):
        assert handles[0].closed
        assert json.loads(Path(source).read_text(encoding='utf-8'))==dict(new=True)
        replacements.append(Path(source))
        if len(replacements)==1:
            assert target.read_text(encoding='utf-8')=='{"old":true}'
            raise denied(destination,winerror)
        return replace(source,destination)
    monkeypatch.setattr(files,'_WINDOWS',True)
    monkeypatch.setattr(files.tempfile,'NamedTemporaryFile',temporary)
    monkeypatch.setattr(files.os,'fsync',synced)
    monkeypatch.setattr(files.os,'replace',contend)
    monkeypatch.setattr(files.time,'sleep',sleeps.append)
    assert files.write_json_report(target,dict(new=True))==target
    assert len(handles)==len(syncs)==1
    assert len(replacements)==2 and len(set(replacements))==1
    assert sleeps==[.01]
    assert json.loads(target.read_text(encoding='utf-8'))==dict(new=True)
    assert not list(tmp_path.glob('*.tmp'))


def test_exhaustion_is_bounded_and_raises_first_error_with_old_target_preserved(tmp_path,monkeypatch):
    target=tmp_path/'report.json'
    target.write_bytes(b'{"old":true}')
    attempts,sleeps=[],[]
    def never_replace(source,destination):
        error=denied(destination,message=f'denial {len(attempts)}')
        attempts.append(error)
        raise error
    monkeypatch.setattr(files,'_WINDOWS',True)
    monkeypatch.setattr(files.os,'replace',never_replace)
    monkeypatch.setattr(files.time,'sleep',sleeps.append)
    with pytest.raises(PermissionError) as caught:
        files.write_json_report(target,dict(new=True))
    assert caught.value is attempts[0] and len(attempts)==5
    assert sleeps==[.01,.02,.04,.08] and sum(sleeps)==pytest.approx(.15)
    assert target.read_bytes()==b'{"old":true}'
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('case',['nonwindows','other-winerror','missing-winerror','other-error'])
def test_other_failures_are_not_retried(tmp_path,monkeypatch,case):
    target=tmp_path/'report.json'
    target.write_bytes(b'{"old":true}')
    error=(OSError(errno.EIO,'device failure') if case=='other-error' else
           denied(target,13 if case=='other-winerror' else None if case=='missing-winerror' else 5))
    if case=='other-error':
        error.winerror=5
    attempts,sleeps=[],[]
    def fail(*args):
        attempts.append(True)
        raise error
    monkeypatch.setattr(files,'_WINDOWS',case!='nonwindows')
    monkeypatch.setattr(files.os,'replace',fail)
    monkeypatch.setattr(files.time,'sleep',sleeps.append)
    with pytest.raises(OSError) as caught:
        files.write_json_report(target,dict(new=True))
    assert caught.value is error
    assert len(attempts)==1 and not sleeps
    assert target.read_bytes()==b'{"old":true}' and not list(tmp_path.glob('*.tmp'))


def test_serialization_failure_does_not_create_or_replace_a_file(tmp_path,monkeypatch):
    target=tmp_path/'report.json'
    target.write_bytes(b'{"old":true}')
    monkeypatch.setattr(files.tempfile,'NamedTemporaryFile',lambda **kwargs:pytest.fail('No tempfile for bad JSON'))
    with pytest.raises(ValueError):
        files.write_json_report(target,dict(invalid=float('nan')))
    assert target.read_bytes()==b'{"old":true}' and not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('permanent_cleanup',[False,True])
def test_cleanup_contention_is_bounded_without_masking_the_write_error(tmp_path,monkeypatch,permanent_cleanup):
    target=tmp_path/'report.json'
    target.write_bytes(b'{"old":true}')
    original=denied(target,message='original replacement failure')
    sleeps,cleanup=[] ,[]
    unlink=Path.unlink
    def fail_replace(*args):
        raise original
    def locked_unlink(path,*args,**kwargs):
        cleanup.append(path)
        if permanent_cleanup or len(cleanup)<3:
            raise denied(path,32,message='cleanup contention')
        return unlink(path,*args,**kwargs)
    monkeypatch.setattr(files,'_WINDOWS',True)
    monkeypatch.setattr(files.os,'replace',fail_replace)
    monkeypatch.setattr(files.time,'sleep',sleeps.append)
    monkeypatch.setattr(Path,'unlink',locked_unlink)
    try:
        with pytest.raises(PermissionError) as caught:
            files.write_json_report(target,dict(new=True))
        assert caught.value is original
        assert target.read_bytes()==b'{"old":true}'
        assert len(cleanup)==(5 if permanent_cleanup else 3)
        assert sleeps[:4]==[.01,.02,.04,.08]
        if permanent_cleanup:
            assert sleeps[4:]==[.01,.02,.04,.08]
            assert len(list(tmp_path.glob('*.tmp')))==1
            assert any('cleanup also failed' in note for note in original.__notes__)
        else:
            assert sleeps[4:]==[.01,.02]
            assert not list(tmp_path.glob('*.tmp'))
    finally:
        for path in tmp_path.glob('*.tmp'):
            unlink(path,missing_ok=True)


@pytest.mark.skipif(os.name!='nt',reason='Requires a genuine Windows sharing lock')
def test_real_windows_target_sharing_lock_can_release_between_atomic_attempts(tmp_path,monkeypatch):
    target=tmp_path/'report.json'
    target.write_bytes(b'{"old":true}')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW
    create.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,
                     ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
    create.restype=ctypes.c_void_p
    close=kernel.CloseHandle
    close.argtypes=[ctypes.c_void_p]
    close.restype=ctypes.c_int
    # Permit reads/writes but deliberately omit FILE_SHARE_DELETE, blocking replace.
    handle=create(str(target),0x80000000,3,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    requested,released=threading.Event(),threading.Event()
    errors=[]
    def release():
        try:
            requested.wait(10)
            if not close(handle):
                errors.append(ctypes.WinError(ctypes.get_last_error()))
        finally:
            released.set()
    thread=threading.Thread(target=release)
    thread.start()
    real_replace=files.os.replace
    denials=[]
    def replace(source,destination):
        try:
            return real_replace(source,destination)
        except PermissionError as error:
            denials.append(error.winerror)
            requested.set()
            assert released.wait(5),'The controlled target lock was not released'
            raise
    monkeypatch.setattr(files.os,'replace',replace)
    try:
        files.write_json_report(target,dict(new=True))
        assert denials and all(code in (5,32,33) for code in denials)
        assert not errors
        assert json.loads(target.read_text(encoding='utf-8'))==dict(new=True)
        assert not list(tmp_path.glob('*.tmp'))
    finally:
        requested.set()
        thread.join(10)
        assert not thread.is_alive()


def test_json_report_implementation_remains_pinned_in_radar_protocol():
    from ops.opportunity_radar import SOURCE_FILES
    assert 'ops/report_files.py' in SOURCE_FILES
