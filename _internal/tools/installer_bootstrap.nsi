; Standard NSIS bootstrap only. All application installation, path validation,
; repair, rollback and removal remain in the existing Python maintenance wizard.
; The executable never downloads or runs a command shell.
Unicode true
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"

!ifndef VERSION
  !error "VERSION is required"
!endif
!ifndef STAGING
  !error "STAGING is required"
!endif
!ifndef OUTPUT
  !error "OUTPUT is required"
!endif
!ifndef APPICON
  !error "APPICON is required"
!endif

Name "文字起こし統合スタジオ ${VERSION}"
Caption "文字起こし統合スタジオ セットアップ"
OutFile "${OUTPUT}"
Icon "${APPICON}"
RequestExecutionLevel user
ManifestDPIAware true
ManifestLongPathAware true
XPStyle on
SetCompressor zlib
SetDatablockOptimize on
CRCCheck force
AutoCloseWindow true
ShowInstDetails nevershow
Page instfiles
LoadLanguageFile "${NSISDIR}\Contrib\Language files\Japanese.nlf"
BrandingText "文字起こし統合スタジオ"
VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=1041 "ProductName" "文字起こし統合スタジオ"
VIAddVersionKey /LANG=1041 "FileDescription" "文字起こし統合スタジオ セットアップ"
VIAddVersionKey /LANG=1041 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1041 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1041 "CompanyName" "Unified Transcriber"
VIAddVersionKey /LANG=1041 "LegalCopyright" "Unified Transcriber contributors"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "このアプリには64ビット版のWindowsが必要です。"
    SetErrorLevel 1
    Quit
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_OK|MB_ICONSTOP "このアプリにはWindows 10以降が必要です。"
    SetErrorLevel 1
    Quit
  ${EndIf}
FunctionEnd

Section "セットアップ画面の準備"
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  DetailPrint "セットアップに必要なファイルを展開しています。"
  File /r "${STAGING}\*"
  IfErrors startup_failed

  ; The Python entry validates the real parent PID and image before accepting
  ; this context. The complete original UTF-16 command line goes directly from
  ; the Win32 pointer to the environment, avoiding NSIS's string-size limit and
  ; any second layer of argument quoting. It is never interpreted by a shell.
  System::Call 'kernel32::SetEnvironmentVariableW(w "UNIFIED_SETUP_ORIGIN", w "$EXEPATH") i .r0'
  StrCmp $0 0 startup_failed
  System::Call 'kernel32::GetCurrentProcessId() i .r0'
  System::Call 'kernel32::SetEnvironmentVariableW(w "UNIFIED_SETUP_PARENT_PID", w r0) i .r1'
  StrCmp $1 0 startup_failed
  System::Call 'kernel32::GetCommandLineW() p .r0'
  StrCmp $0 0 startup_failed
  System::Call 'kernel32::SetEnvironmentVariableW(w "UNIFIED_SETUP_COMMAND_LINE", p r0) i .r1'
  StrCmp $1 0 startup_failed

  DetailPrint "セットアップ画面を開いています。"
  ; The extracting bootstrap must remain alive as the wizard's verified parent,
  ; but its completed extraction progress is no longer useful while the
  ; interactive wizard is running. Do not leave a second, apparently stuck
  ; window in front of it. ExecWait still preserves the parent and exit code.
  HideWindow
  ClearErrors
  ExecWait '"$PLUGINSDIR\Runtime\pythonw.exe" -I -X utf8 "$PLUGINSDIR\_internal\installer_bootstrap.py"' $0
  IfErrors startup_failed
  SetErrorLevel $0
  Goto finished

startup_failed:
  ShowWindow $HWNDPARENT 5 ; SW_SHOW: make an extraction/startup failure visible
  MessageBox MB_OK|MB_ICONSTOP "セットアップを開始できませんでした。ファイルの保存先と空き容量を確認し、公式の配布元から再ダウンロードしてください。"
  SetErrorLevel 1
finished:
SectionEnd
