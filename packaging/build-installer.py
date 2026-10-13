#!/usr/bin/env python3
"""Build a per-user, unsigned NSIS candidate installer without executing Windows code.

Uses the existing native Linux NSIS compiler and explicit payload manifest. Does not
modify electron-builder, existing installations, configuration, or source payload.
Windows install/uninstall/runtime acceptance is a separate required step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def nsis_string(value: str) -> str:
    if any(c in value for c in ("\r", "\n", "\0")):
        raise ValueError("NSIS values must be single-line")
    return value.replace("$", "$$").replace('"', '$\\"')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--nsis-dir", required=True, type=Path)
    parser.add_argument("--nsis-resources", required=True, type=Path)
    parser.add_argument("--display-name", default="BioMind Studio Connection Candidate")
    parser.add_argument("--probe", action="store_true", help="Mark output as NOT_FOR_DELIVERY")
    args = parser.parse_args()
    payload = args.payload.resolve(strict=True)
    output = args.output.resolve()
    if not payload.is_dir() or output == payload or payload in output.parents:
        raise ValueError("Output must be outside payload")
    if Path(args.exe).name != args.exe or not (payload / args.exe).is_file():
        raise ValueError("--exe must identify a file at payload root")
    if output.exists():
        raise ValueError("Use a fresh output directory")
    entries = []
    for path in sorted(payload.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink in installer payload: {path}")
        if path.is_file():
            entries.append({"path": path.relative_to(payload).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)})
    if not entries:
        raise ValueError("Empty payload")
    output.mkdir(parents=True)
    installer_name = f"BioMind-Studio-{args.version}-win-x64-Setup.exe"
    if args.probe:
        installer_name = "NOT_FOR_DELIVERY-" + installer_name
    installer = output / installer_name
    nsis_dir = args.nsis_dir.resolve(strict=True)
    nsis_resources = args.nsis_resources.resolve(strict=True)
    install_dir = "BioMind Studio Workbench"
    uninstall_key = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\BioMindStudioWorkbench"
    text = [
        'Unicode true',
        '!include "MUI2.nsh"',
        '!include "LogicLib.nsh"',
        '!include "x64.nsh"',
        f'!addplugindir "{nsis_string(str(nsis_resources / "plugins/x86-unicode"))}"',
        f'Name "{nsis_string(args.display_name)}"',
        f'OutFile "{nsis_string(str(installer))}"',
        'RequestExecutionLevel user',
        'SetCompressor /SOLID lzma',
        'SetCompressorDictSize 32',
        'ShowInstDetails show',
        'ShowUninstDetails show',
        f'InstallDir "$LOCALAPPDATA\\Programs\\{install_dir}"',
        f'InstallDirRegKey HKCU "{uninstall_key}" "InstallLocation"',
        '!define MUI_ABORTWARNING',
        '!insertmacro MUI_PAGE_WELCOME',
        '!insertmacro MUI_PAGE_DIRECTORY',
        '!insertmacro MUI_PAGE_INSTFILES',
        '!insertmacro MUI_PAGE_FINISH',
        '!insertmacro MUI_UNPAGE_CONFIRM',
        '!insertmacro MUI_UNPAGE_INSTFILES',
        '!insertmacro MUI_LANGUAGE "English"',
        '!insertmacro MUI_LANGUAGE "SimpChinese"',
        'Function .onInit',
        '  ${IfNot} ${RunningX64}',
        '    MessageBox MB_OK|MB_ICONSTOP "This build requires 64-bit Windows."',
        '    Abort',
        '  ${EndIf}',
        '  SetShellVarContext current',
        '  loop_app:',
        f'    nsProcess::_FindProcess "{nsis_string(args.exe)}"',
        '    Pop $0',
        '    ${If} $0 == 0',
        '      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Please close BioMind Studio before installing. Remote tasks will remain on the server." IDRETRY loop_app',
        '      Abort',
        '    ${EndIf}',
        'FunctionEnd',
        'Function un.onInit',
        '  SetShellVarContext current',
        '  loop_app:',
        f'    nsProcess::_FindProcess "{nsis_string(args.exe)}"',
        '    Pop $0',
        '    ${If} $0 == 0',
        '      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Please close BioMind Studio before uninstalling. Saved connection settings are retained." IDRETRY loop_app',
        '      Abort',
        '    ${EndIf}',
        'FunctionEnd',
        'Section "Install"',
        '  SetShellVarContext current',
    ]
    current_directory = None
    directories = set()
    for item in entries:
        relative = Path(item["path"])
        parent = relative.parent.as_posix()
        if parent != current_directory:
            suffix = "" if parent == "." else "\\" + nsis_string(parent.replace("/", "\\"))
            text.append(f'  SetOutPath "$INSTDIR{suffix}"')
            current_directory = parent
        text.append(f'  File "{nsis_string(str(payload / relative))}"')
        for directory in relative.parents:
            if str(directory) != ".":
                directories.add(directory.as_posix())
    text += [
        '  SetOutPath "$INSTDIR"',
        '  WriteUninstaller "$INSTDIR\\Uninstall.exe"',
        f'  CreateShortcut "$DESKTOP\\{install_dir}.lnk" "$INSTDIR\\{nsis_string(args.exe)}"',
        f'  CreateShortcut "$SMPROGRAMS\\{install_dir}.lnk" "$INSTDIR\\{nsis_string(args.exe)}"',
        f'  WriteRegStr HKCU "{uninstall_key}" "DisplayName" "{nsis_string(args.display_name)}"',
        f'  WriteRegStr HKCU "{uninstall_key}" "DisplayVersion" "{nsis_string(args.version)}"',
        f'  WriteRegStr HKCU "{uninstall_key}" "Publisher" "BioMind"',
        f'  WriteRegStr HKCU "{uninstall_key}" "InstallLocation" "$INSTDIR"',
        f'  WriteRegStr HKCU "{uninstall_key}" "UninstallString" \'"$INSTDIR\\Uninstall.exe"\'',
        f'  WriteRegStr HKCU "{uninstall_key}" "DisplayIcon" \'"$INSTDIR\\{nsis_string(args.exe)}"\'',
        f'  WriteRegDWORD HKCU "{uninstall_key}" "EstimatedSize" {(sum(x["bytes"] for x in entries) + 1023) // 1024}',
        f'  WriteRegDWORD HKCU "{uninstall_key}" "NoModify" 1',
        f'  WriteRegDWORD HKCU "{uninstall_key}" "NoRepair" 1',
        'SectionEnd',
        'Section "Uninstall"',
        '  SetShellVarContext current',
    ]
    for item in entries:
        windows_relative = nsis_string(item["path"].replace("/", "\\"))
        text.append(f'  Delete "$INSTDIR\\{windows_relative}"')
    text.append('  Delete "$INSTDIR\\Uninstall.exe"')
    for directory in sorted(directories, key=lambda p: (p.count("/"), p), reverse=True):
        windows_directory = nsis_string(directory.replace("/", "\\"))
        text.append(f'  RMDir "$INSTDIR\\{windows_directory}"')
    text += [
        '  RMDir "$INSTDIR"',
        f'  Delete "$DESKTOP\\{install_dir}.lnk"',
        f'  Delete "$SMPROGRAMS\\{install_dir}.lnk"',
        f'  DeleteRegKey HKCU "{uninstall_key}"',
        'SectionEnd',
    ]
    script = output / "installer.nsi"
    script.write_text("\n".join(text) + "\n", encoding="utf-8")
    (output / "payload-manifest.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n")
    command = [str(nsis_dir / "linux/makensis"), "-WX", "-INPUTCHARSET", "UTF8", str(script)]
    env = dict(os.environ, NSISDIR=str(nsis_dir))
    with (output / "stdout.log").open("w") as stdout, (output / "stderr.log").open("w") as stderr:
        result = subprocess.run(command, stdout=stdout, stderr=stderr, env=env, timeout=600)
    receipt = {"schema": "BioMindNativeNSISBuildV1", "command": command, "exitCode": result.returncode,
               "payload": str(payload), "payloadFiles": len(entries), "probeOnly": args.probe,
               "usesWine": False, "windowsInstall": "NOT_RUN", "windowsUninstall": "NOT_RUN", "windowsRuntime": "NOT_RUN",
               "scriptSha256": sha256(script), "nsisCompilerSha256": sha256(nsis_dir / "linux/makensis")}
    if result.returncode == 0:
        receipt["installer"] = {"path": str(installer), "bytes": installer.stat().st_size, "sha256": sha256(installer)}
    (output / "result.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
