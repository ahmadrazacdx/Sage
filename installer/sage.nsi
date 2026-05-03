; ==========================================================================
; Sage NSIS Installer Script
;
; Two-file architecture to bypass the 32-bit NSIS 2 GB mmap limit:
;   sage-<tier>-<ver>.exe   -- this stub (~20 MB, embeds only config + frontend)
;   sage-<tier>-<ver>.bin   -- payload tar (~2.5 GB, python + models + binaries)
;
; The installer checks that the .bin file is present next to the .exe, then
; extracts it with Windows tar into $INSTDIR.  Once installation is complete
; the user can safely delete both the .exe and .bin — the app is self-contained.
;
; Compile-time defines (all supplied by build.ps1):
;   /DTIER=fast|pro|fast-lite|pro-lite
;   /DVERSION=x.y.z
;   /DSTAGING_DIR=path\to\staging
;   /DOUTPUT_FILE=path\to\output.exe
;   /DBACKEND=cpu|cuda
;   /DPAYLOAD_NAME=sage-<tier>-<ver>-windows-x86_64.bin
; ==========================================================================

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "nsDialogs.nsh"

; ---------- compression (only affects the small files embedded in the stub) ----------
SetCompressor /SOLID lzma
SetCompressorDictSize 64

; ---------- basic info ----------
!define PRODUCT_NAME      "Sage"
!define PRODUCT_PUBLISHER  "Sage Project"
!define PRODUCT_WEB_SITE   "https://github.com/ahmadrazacdx/Sage"
!define UNINSTALL_REG_KEY  "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"

Name "${PRODUCT_NAME} ${VERSION} (${TIER})"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\${PRODUCT_NAME}"
InstallDirRegKey HKCU "${UNINSTALL_REG_KEY}" "InstallLocation"
RequestExecutionLevel user
ShowInstDetails show
ShowUninstDetails show

; ---------- version info ----------
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName"     "${PRODUCT_NAME}"
VIAddVersionKey "ProductVersion"  "${VERSION}"
VIAddVersionKey "CompanyName"     "${PRODUCT_PUBLISHER}"
VIAddVersionKey "FileDescription" "Sage Academic Assistant Installer"
VIAddVersionKey "FileVersion"     "${VERSION}"
VIAddVersionKey "LegalCopyright"  "Apache-2.0"

; ---------- MUI settings ----------
!define MUI_ABORTWARNING
!define MUI_ICON "${STAGING_DIR}\..\..\sage.ico"

!define MUI_WELCOMEPAGE_TITLE "Welcome to Sage ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "This will install Sage Academic Assistant (${TIER} edition) on your computer.$\r$\n$\r$\nSage is an offline-first, AI-powered academic assistant.$\r$\n$\r$\nClick Next to continue."

!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Launch Sage"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchSage

; ---------- pages ----------
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${STAGING_DIR}\..\..\..\LICENSE.md"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ---------- variables ----------
Var InstallLog

; ==========================================================================
; INSTALL SECTIONS
; ==========================================================================

Section "Core Files" SecCore
    SectionIn RO ; required, cannot deselect

    SetOutPath "$INSTDIR"

    ; --- Install log ---
    CreateDirectory "$INSTDIR\logs"
    StrCpy $InstallLog "$INSTDIR\logs\install.log"
    FileOpen $0 $InstallLog w
    FileWrite $0 "Sage Install Log$\r$\n"
    FileWrite $0 "Version: ${VERSION}$\r$\n"
    FileWrite $0 "Tier: ${TIER}$\r$\n"
    FileWrite $0 "Backend: ${BACKEND}$\r$\n"
    FileClose $0

    ; Write install-pending marker (removed on success)
    FileOpen $0 "$INSTDIR\_install_pending" w
    FileWrite $0 "Installation in progress"
    FileClose $0

    ; --- Config (small, embedded directly in installer) ---
    DetailPrint "Installing configuration..."
    SetOutPath "$INSTDIR\config"
    File /r "${STAGING_DIR}\config\*.*"

    ; --- Frontend (small, embedded directly in installer) ---
    DetailPrint "Installing frontend..."
    SetOutPath "$INSTDIR\frontend\artifacts\sage\dist"
    File /r "${STAGING_DIR}\frontend\artifacts\sage\dist\*.*"

    ; --- Install manifest ---
    SetOutPath "$INSTDIR"
    File "${STAGING_DIR}\manifest.json"

    ; --- Data directories (empty structure, created by installer) ---
    CreateDirectory "$INSTDIR\artifacts\data\databases"
    CreateDirectory "$INSTDIR\artifacts\data\exports"
    CreateDirectory "$INSTDIR\artifacts\data\processed"
    CreateDirectory "$INSTDIR\artifacts\data\raw"
    CreateDirectory "$INSTDIR\artifacts\sandbox\data\sessions"
    CreateDirectory "$INSTDIR\artifacts\sandbox\data\figures"
    CreateDirectory "$INSTDIR\logs"

    ; --- Branding Icon ---
    SetOutPath "$INSTDIR"
    File "${STAGING_DIR}\..\..\sage.ico"

    ; --- Launcher script (VBScript for 100% silent startup) ---
    FileOpen $0 "$INSTDIR\Sage.vbs" w
    FileWrite $0 'Set fso = CreateObject("Scripting.FileSystemObject")$\r$\n'
    FileWrite $0 'Set WshShell = CreateObject("WScript.Shell")$\r$\n'
    FileWrite $0 'pythonPath = "$INSTDIR\python\pythonw.exe"$\r$\n'
    FileWrite $0 'If Not fso.FileExists(pythonPath) Then$\r$\n'
    FileWrite $0 '  MsgBox "Python runtime not found at: " & pythonPath, 16, "Sage Startup Error"$\r$\n'
    FileWrite $0 '  WScript.Quit 1$\r$\n'
    FileWrite $0 'End If$\r$\n'
    FileWrite $0 'WshShell.Environment("PROCESS")("SAGE_HOME") = "$INSTDIR"$\r$\n'
    FileWrite $0 'WshShell.Run Chr(34) & pythonPath & Chr(34) & " -m sage", 0, False$\r$\n'
    FileClose $0

    ; =======================================================================
    ; PAYLOAD EXTRACTION
    ; Python runtime, LLM models, and server binaries are too large to embed
    ; in the installer.  They are shipped in a companion .bin file (plain tar)
    ; that must be in the same directory as this .exe when running Setup.
    ; After extraction everything is self-contained in $INSTDIR — the .bin
    ; and .exe can be deleted without affecting the installed application.
    ; =======================================================================
    DetailPrint "Checking for payload archive (${PAYLOAD_NAME})..."
    IfFileExists "$EXEDIR\${PAYLOAD_NAME}" payload_ok payload_missing

    payload_missing:
        MessageBox MB_OK|MB_ICONSTOP \
            "Required setup file not found:$\r$\n$\r$\n  $EXEDIR\${PAYLOAD_NAME}$\r$\n$\r$\nPlease ensure both files from the downloaded .zip archive are in the same folder as this installer, then re-run Setup."
        Abort

    payload_ok:
    DetailPrint "Extracting payload (Python runtime, models, binaries) — this may take several minutes..."
    nsExec::ExecToLog '"$WINDIR\System32\cmd.exe" /c tar -xf "$EXEDIR\${PAYLOAD_NAME}" -C "$INSTDIR"'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_OK|MB_ICONSTOP \
            "Payload extraction failed with error code $0.$\r$\n$\r$\nPossible causes: insufficient disk space, corrupted download, or antivirus interference.$\r$\nPlease re-download the archive and try again."
        Abort
    ${EndIf}
    DetailPrint "Payload extracted successfully."

SectionEnd

; ==========================================================================
; POST-INSTALL
; ==========================================================================
Section "-PostInstall" SecPost

    ; --- Windows Defender exclusion (best effort, no UAC) ---
    DetailPrint "Requesting Defender exclusion (may require admin)..."
    nsExec::ExecToLog 'powershell -NoProfile -Command "try { Add-MpPreference -ExclusionPath \"$INSTDIR\" -ErrorAction SilentlyContinue } catch {}"'

    ; --- Registry: Add/Remove Programs ---
    WriteRegStr   HKCU "${UNINSTALL_REG_KEY}" "DisplayName"     "${PRODUCT_NAME} (${TIER})"
    WriteRegStr   HKCU "${UNINSTALL_REG_KEY}" "DisplayVersion"  "${VERSION}"
    WriteRegStr   HKCU "${UNINSTALL_REG_KEY}" "Publisher"        "${PRODUCT_PUBLISHER}"
    WriteRegStr   HKCU "${UNINSTALL_REG_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr   HKCU "${UNINSTALL_REG_KEY}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
    WriteRegStr   HKCU "${UNINSTALL_REG_KEY}" "URLInfoAbout"    "${PRODUCT_WEB_SITE}"
    WriteRegDWORD HKCU "${UNINSTALL_REG_KEY}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINSTALL_REG_KEY}" "NoRepair" 1

    ; Calculate installed size for Add/Remove Programs
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKCU "${UNINSTALL_REG_KEY}" "EstimatedSize" $0

    ; --- Shortcuts ---
    CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"

    CreateShortcut "$DESKTOP\Sage.lnk" \
        "$WINDIR\System32\wscript.exe" \
        "$\"$INSTDIR\Sage.vbs$\"" \
        "$INSTDIR\sage.ico" 0 SW_SHOWNORMAL "" \
        "Sage Academic Assistant"

    CreateShortcut "$SMPROGRAMS\${PRODUCT_NAME}\Sage.lnk" \
        "$WINDIR\System32\wscript.exe" \
        "$\"$INSTDIR\Sage.vbs$\"" \
        "$INSTDIR\sage.ico" 0 SW_SHOWNORMAL "" \
        "Sage Academic Assistant"

    CreateShortcut "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall.lnk" \
        "$INSTDIR\uninstall.exe"

    ; --- Set SAGE_HOME as user env var ---
    WriteRegStr HKCU "Environment" "SAGE_HOME" "$INSTDIR"
    SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=500

    ; --- Write uninstaller ---
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; --- Remove install-pending marker ---
    Delete "$INSTDIR\_install_pending"

    ; --- Final log entry ---
    FileOpen $0 "$INSTDIR\logs\install.log" a
    FileSeek $0 0 END
    FileWrite $0 "Installation completed successfully$\r$\n"
    FileClose $0

    DetailPrint "Installation complete!"

SectionEnd

; ==========================================================================
; LAUNCH FUNCTION
; ==========================================================================
Function LaunchSage
    Exec 'wscript.exe "$INSTDIR\Sage.vbs"'
FunctionEnd

; ==========================================================================
; PRE-INSTALL CHECKS
; ==========================================================================
Function .onInit

    ; --- Check for existing installation ---
    ReadRegStr $0 HKCU "${UNINSTALL_REG_KEY}" "InstallLocation"
    ${If} $0 != ""
        ${If} ${FileExists} "$0\uninstall.exe"
            MessageBox MB_YESNO|MB_ICONQUESTION \
                "Sage is already installed at $0.$\r$\n$\r$\nDo you want to uninstall the previous version first?" \
                IDYES uninst_prev IDNO skip_uninst
            uninst_prev:
                ExecWait '"$0\uninstall.exe" /S'
                Sleep 2000
            skip_uninst:
        ${EndIf}
    ${EndIf}

    ; --- Disk space check (payload + installed overhead) ---
!if "${TIER}" == "fast"
    StrCpy $1 "4000"
!else if "${TIER}" == "pro"
    StrCpy $1 "7000"
!else if "${TIER}" == "fast-lite"
    StrCpy $1 "1000"
!else if "${TIER}" == "pro-lite"
    StrCpy $1 "2000"
!endif

    ${GetRoot} "$INSTDIR" $2
    ${DriveSpace} "$2\" "/D=F /S=M" $3
    ${If} $3 < $1
        MessageBox MB_OK|MB_ICONSTOP \
            "Not enough disk space.$\r$\n$\r$\nRequired: $1 MB$\r$\nAvailable: $3 MB on $2"
        Abort
    ${EndIf}

FunctionEnd

; ==========================================================================
; UNINSTALLER
; ==========================================================================
Section "Uninstall"

    ; --- Remove files ---
    DetailPrint "Removing files..."
    RMDir /r "$INSTDIR\python"
    RMDir /r "$INSTDIR\config"
    RMDir /r "$INSTDIR\frontend"
    RMDir /r "$INSTDIR\artifacts"
    RMDir /r "$INSTDIR\logs"
    Delete "$INSTDIR\manifest.json"
    Delete "$INSTDIR\Sage.cmd"
    Delete "$INSTDIR\launch-sage.cmd"
    Delete "$INSTDIR\_install_pending"
    Delete "$INSTDIR\uninstall.exe"
    RMDir "$INSTDIR"

    ; --- Remove shortcuts ---
    Delete "$DESKTOP\Sage.lnk"
    Delete "$SMPROGRAMS\${PRODUCT_NAME}\Sage.lnk"
    Delete "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall.lnk"
    RMDir "$SMPROGRAMS\${PRODUCT_NAME}"

    ; --- Remove registry ---
    DeleteRegKey HKCU "${UNINSTALL_REG_KEY}"
    DeleteRegValue HKCU "Environment" "SAGE_HOME"
    SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=500

    ; --- Remove Defender exclusion ---
    nsExec::ExecToLog 'powershell -NoProfile -Command "try { Remove-MpPreference -ExclusionPath \"$INSTDIR\" -ErrorAction SilentlyContinue } catch {}"'

    DetailPrint "Uninstall complete."

SectionEnd
