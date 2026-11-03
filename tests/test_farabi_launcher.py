"""The offline runtime check must never trigger the installer."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('powershell.exe') is None, reason='Windows PowerShell is unavailable')
@pytest.mark.parametrize('check_only', [True, False])
@pytest.mark.parametrize('watch', [True, False])
def test_missing_runtime_check_does_not_run_installer(tmp_path, check_only, watch):
    root = Path(__file__).resolve().parents[1]
    scripts = tmp_path / 'scripts'
    capture = tmp_path / 'capture'
    scripts.mkdir()
    capture.mkdir()
    shutil.copyfile(root / 'scripts' / 'capture_farabi.ps1', scripts / 'capture_farabi.ps1')
    shutil.copyfile(root / 'capture' / 'runtime-lock.json', capture / 'runtime-lock.json')
    marker = scripts / 'installer-invoked'
    (scripts / 'setup_farabi.ps1').write_text(
        "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'installer-invoked') -Value 'called'\nexit 7\n",
        encoding='utf-8',
    )
    env = os.environ.copy()
    env.pop('BRS_NODE', None)
    env.pop('BRS_PLAYWRIGHT_MODULE', None)
    env.pop('PROCESSOR_ARCHITEW6432', None)
    env['PROCESSOR_ARCHITECTURE'] = 'AMD64'
    command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
               str(scripts / 'capture_farabi.ps1')]
    if check_only:
        command.append('-Check')
    if watch:
        command.append('-Watch')
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, timeout=30, check=False)
    assert result.returncode != 0
    assert marker.exists() is not check_only
