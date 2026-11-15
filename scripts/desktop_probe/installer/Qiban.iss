; SPDX-License-Identifier: MIT
; INSTALL-174 + UPGRADE-177: per-user installer for one already assembled Qiban application
; directory.
;
; This script copies exactly what assemble_application.py produced - it never packs the project,
; never inspects a profile, never creates a database and never moves personal data. It supports
; four shapes, decided from the target directory and the machine's own records only:
;
;   1. fresh install      an empty/new directory: plain file copy, nothing else;
;   2. same-build repair  the identical published identity: overwrite the same file set;
;   3. never-run replace  a different build while the default profile has never created an
;                         installation: plain program replacement, no data pairing exists;
;   4. paired upgrade     a different build over a ready installation: the *installed* build's
;                         own frozen worker runs the existing `--upgrade-maintenance prepare`
;                         verb first (see installer/prepare_upgrade.ps1), and only after that
;                         answer the new files are copied. The new program then confirms the
;                         pairing on its first start; the previous build's copy stays recorded
;                         so the way back exists even when the new backend cannot start.
;
; Everything that needs reasoning about metadata lives in the embedded PowerShell glue
; (prepare_upgrade.ps1, extracted to a temporary directory, run hidden with -NoProfile
; -NonInteractive). This script never parses JSON, never starts Qiban.exe for maintenance and
; never talks to the database. It only reads that script's ASCII status/error result, and a
; non-zero result means "refuse before any file is written".
;
; "The same build" is decided by the qiban-payload.json identity that build_installer.py writes
; beside the setup and that this script installs into {app}: one deterministic list of every
; published file with its byte count and SHA-256. The digest of that document is compiled in as a
; macro. A target directory that holds 栖伴 must carry the same identity, otherwise setup refuses
; to overwrite it. The installed files themselves are never re-hashed here - damaged or missing
; payload files are exactly what a repair has to replace.
;
; A build that does not know the maintenance verb (everything up to and including 176) exits
; non-zero with an argument error; setup then refuses to overwrite the directory and says so,
; instead of inventing a pairing.
;
; Compiled only through build_installer.py, which validates the payload and supplies six macros.
; Do not compile by hand: without them ISCC stops below.
;
;   PayloadDir                  assembled application directory (contains Qiban.exe)
;   OutputDir                   separate build output directory
;   PayloadIdentity             qiban-payload.json written by build_installer.py
;   Version                     development version, e.g. 0.9.4.174 (never a fake 1.0)
;   ExpectedBuildRecordSha256   SHA-256 of that payload's desktop-build.json
;   ExpectedPayloadIdentitySha256  SHA-256 of that qiban-payload.json
;
; Inno Setup itself is redistributed under its own license; see
; https://jrsoftware.org/files/is/license.txt

#ifndef PayloadDir
  #error PayloadDir is required; compile through build_installer.py
#endif
#ifndef OutputDir
  #error OutputDir is required; compile through build_installer.py
#endif
#ifndef PayloadIdentity
  #error PayloadIdentity is required; compile through build_installer.py
#endif
#ifndef Version
  #error Version is required; compile through build_installer.py
#endif
#ifndef ExpectedBuildRecordSha256
  #error ExpectedBuildRecordSha256 is required; compile through build_installer.py
#endif
#ifndef ExpectedPayloadIdentitySha256
  #error ExpectedPayloadIdentitySha256 is required; compile through build_installer.py
#endif

#define AppName "栖伴"
#define AppExeName "Qiban.exe"
; UNINSTALL: the published uninstall entry. It is one byte-identical copy of AppExeName (verified by
; assemble_application.py and again by build_installer.py) and only forwards to
; "Qiban.exe --uninstall", the application's own preparation page.
#define UninstallAliasName "uninstall.exe"
#define PreparationSwitch "--uninstall"
; The one fixed internal parameter the preparation page adds when it starts this setup's uninstaller
; again after the application and its database were closed. Nothing else may mean "already prepared".
#define PreparedParameter "QIBANPREPARED"
#define BuildRecordName "desktop-build.json"
#define PayloadIdentityName "qiban-payload.json"
; UPGRADE-177: the embedded maintenance glue and the directory shape of one installed profile.
#define GlueScriptName "prepare_upgrade.ps1"
; A byte-identical copy of this build's qiban-payload.json under a setup-only name. The setup embeds
; it (never installs it) so the glue always receives this package's own published manifest as one
; unambiguous file path. build_installer.py writes it beside qiban-payload.json and records both.
#define PayloadManifestName "qiban-target-payload.json"
#define RecoveryLinkName "栖伴升级回退.lnk"
#define RecoveryLinkTitle "栖伴升级回退"
; Where the installed application keeps its maintenance root: AppPaths.Root is the profile
; directory, and the desktop installation (installation.json, database, upgrade records and the
; previous-build copies) lives below <profile>\data\installation.
#define InstallationSubPath "data\installation"
; The application creates this mutex for its whole GUI lifetime and never waits on it, so it says
; only that a window is alive. It is not used to serialize several test profiles.
#define RunningMutexName "Local\QibanDesktopApplicationRunning"
#define OutputFileName "Qiban-Setup-" + Version

[Setup]
; Fixed identity: every build of this package installs into the same registration and upgrades the
; same uninstall entry instead of adding a second application.
AppId={{9C1F0A53-2C4E-4A7B-9B1E-6D2F8A5C1E74}
AppName={#AppName}
AppVersion={#Version}
AppVerName={#AppName} {#Version}
AppPublisher=栖伴
VersionInfoVersion={#Version}
VersionInfoProductVersion={#Version}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} 安装程序
; Current user only: no elevation, no machine-wide state, no Program Files.
PrivilegesRequired=lowest
SetupArchitecture=x64
; Matches the desktop project's SupportedOSPlatformVersion (10.0.18362.0).
MinVersion=10.0.18362
DefaultDirName={localappdata}\Programs\Qiban
; A re-run repairs the previous directory instead of asking for a new one.
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
DefaultGroupName={#AppName}
AllowNoIcons=yes
; The running marker only reports that a window exists. Setup must never close or restart the
; application (nor its database): the person exits 栖伴 from inside the window.
AppMutex={#RunningMutexName}
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile=..\..\..\LICENSE
InfoAfterFile=INSTALL-NOTES.txt
OutputDir={#OutputDir}
OutputBaseFilename={#OutputFileName}
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
ShowLanguageDialog=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Messages]
; Say plainly what must happen: leave through the application, never force the process or
; the database. Setup and Uninstall both stop here while the marker exists.
SetupAppRunningError=安装程序检测到栖伴仍在运行，因此无法继续。%n%n请先回到栖伴窗口，用界面或托盘中的正常退出流程结束栖伴（不要强制结束进程，也不要停止数据库），然后再点击“确定”继续安装或修复。
UninstallAppRunningError=卸载程序检测到栖伴仍在运行，因此无法继续。%n%n请先回到栖伴窗口，用界面或托盘中的正常退出流程结束栖伴，然后再点击“确定”继续卸载。个人资料、备份与凭据不会被删除。
ConfirmUninstall=卸载只会删除安装器登记的程序文件和快捷方式（以及它创建的“栖伴升级回退”入口）。%n%n个人资料（Windows 用户资料目录下的 Qiban\Profile）、备份与凭据会保留，程序目录之外的任何内容都不会被删除。%n%n确认卸载 %1？

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: unchecked

[Files]
; Whitelist: the assembled application directory only, unchanged and recursive. build_installer.py
; refuses a directory that carries project sources or personal data, so nothing else can be picked up.
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The installer's own metadata: the complete published-file identity of this build. It is installed
; so the next run can prove "same build" from one record instead of re-hashing the target's files.
Source: "{#PayloadIdentity}"; DestDir: "{app}"; DestName: "{#PayloadIdentityName}"; Flags: ignoreversion
; UPGRADE-177: the maintenance glue and this package's own published manifest are embedded in the
; setup and only extracted into a temporary directory while setup runs. Neither is installed into
; the program directory; the glue is the only place that reads metadata or starts the worker.
Source: "{#GlueScriptName}"; Flags: dontcopy
Source: "{#OutputDir}\{#PayloadManifestName}"; Flags: dontcopy

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
; UNINSTALL: the Start Menu entry is the stable published alias (one byte-identical copy of the
; program), which only forwards to the application's own preparation page. The Windows application
; list still runs this setup's uninstaller, which opens that same page first (see InitializeUninstall).
Name: "{group}\卸载{#AppName}"; Filename: "{app}\{#UninstallAliasName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动栖伴"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
; UPGRADE-177: exactly one entry. The upgrade recovery shortcut is created by the maintenance glue
; (not by an [Icons] entry), so it is named here or an uninstall would leave it behind. Nothing
; else is deleted: program files come from the install log, personal data lives outside {app}.
Type: files; Name: "{group}\{#RecoveryLinkName}"

[Code]
const
  RunningMutexName = '{#RunningMutexName}';
  BuildRecordName = '{#BuildRecordName}';
  PayloadIdentityName = '{#PayloadIdentityName}';
  AppExeName = '{#AppExeName}';
  UninstallAliasName = '{#UninstallAliasName}';
  PreparationSwitch = '{#PreparationSwitch}';
  PreparedParameter = '{#PreparedParameter}';
  ExpectedRecordSha256 = '{#ExpectedBuildRecordSha256}';
  ExpectedIdentitySha256 = '{#ExpectedPayloadIdentitySha256}';
  GlueScriptName = '{#GlueScriptName}';
  PayloadManifestName = '{#PayloadManifestName}';
  RecoveryLinkName = '{#RecoveryLinkName}';
  RecoveryLinkTitle = '{#RecoveryLinkTitle}';
  InstallationSubPath = '{#InstallationSubPath}';
  UpgradeResultName = 'qiban-upgrade-result.txt';

var
  // Set only when this run really prepared (or continued) a paired upgrade transaction; the
  // forward clean-up after the file copy is meaningless for every other shape.
  UpgradeFinalizeNeeded: Boolean;

function RunningRefusal: String;
begin
  Result := '栖伴仍在运行，因此无法继续。' + #13#10 + #13#10 +
    '请先回到栖伴窗口，用界面或托盘中的正常退出流程结束栖伴' + #13#10 +
    '（不要强制结束进程，也不要停止数据库），然后再重试。';
end;

function MissingIdentityRefusal: String;
begin
  Result := '目标目录里已经有栖伴，但没有本安装包格式的发布身份记录' + #13#10 +
    '（' + PayloadIdentityName + '）。' + #13#10 + #13#10 +
    '安装器不会给旧目录补造身份，也不会覆盖无法确认来源的程序。' + #13#10 +
    '请改用与该目录内版本配套的维护包，或先人工确认这个目录的来历。';
end;

function RecordRefusal: String;
begin
  Result := '目标目录里的栖伴构建记录与这个安装包不一致。' + #13#10 + #13#10 +
    '发布身份与本包相同，但构建记录是另一个，这个目录里的两处记录互相矛盾。' + #13#10 +
    '安装包不会覆盖这种目录；请先人工确认这个目录的来历，' + #13#10 +
    '或用与该目录内版本配套的安装包把它修复回原样。';
end;

function UnrecordedRefusal: String;
begin
  Result := '目标目录里已经有栖伴程序，但没有有效的构建记录' + #13#10 +
    '（desktop-build.json）。' + #13#10 + #13#10 +
    '安装包不会覆盖来历不明的程序。' + #13#10 +
    '请改用与该构建匹配的修复包；如果不确定这个目录从哪里来，' + #13#10 +
    '请先人工确认，不要直接让安装包覆盖。';
end;

// ---------------------------------------------------------------------------------------------
// UPGRADE-177 glue. All metadata work (published identities, the installation record, the upgrade
// transaction, the previous-build copy and the forward clean-up) happens in the embedded
// prepare_upgrade.ps1; this script only resolves the two paths it must decide itself, starts that
// script hidden, reads its ASCII status/error result and turns the code into a fixed sentence.
// ---------------------------------------------------------------------------------------------

// The profile directory is the application's AppPaths.Root. An explicit /DataRoot= switch exists
// for scripted acceptance runs only: it names the *profile* directory, and the installation root
// below is always derived from it, so nobody can pass a bare data directory by accident.
function ProfileDirectory: String;
var
  Given, Value: String;
begin
  // An explicitly given root is never replaced by the default profile: the default profile may be
  // this person's everyday data, so a wrong switch must stop the install, not silently retarget it.
  try
    Given := ExpandConstant('{param:DataRoot|}');
  except
    Result := '';
    Exit;
  end;
  if Given = '' then
    Value := ExpandConstant('{localappdata}\Qiban\Profile')
  else
    Value := Given;
  if (Length(Value) < 3) or (Value[2] <> ':') or (Pos('"', Value) > 0) then
  begin
    Result := '';
    Exit;
  end;
  Result := RemoveBackslashUnlessRoot(Value);
end;

function InstallationRootDirectory: String;
var
  Profile: String;
begin
  Profile := ProfileDirectory;
  if Profile = '' then
  begin
    Result := '';
    Exit;
  end;
  Result := AddBackslash(Profile) + InstallationSubPath;
end;

function RecoveryShortcutPath: String;
begin
  Result := ExpandConstant('{group}') + '\' + RecoveryLinkName;
end;

function GlueResultPath: String;
begin
  Result := AddBackslash(ExpandConstant('{tmp}')) + UpgradeResultName;
end;

// One key of the glue's ASCII result file: "$key=value" lines, nothing else is read.
function ReadGlueValue(Key: String): String;
var
  Lines: TStringList;
  Prefix: String;
  Index: Integer;
begin
  Result := '';
  if not FileExists(GlueResultPath) then
    Exit;
  Lines := TStringList.Create;
  try
    try
      Lines.LoadFromFile(GlueResultPath);
    except
      Exit;
    end;
    Prefix := Key + '=';
    for Index := 0 to Lines.Count - 1 do
      if Pos(Prefix, Lines[Index]) = 1 then
      begin
        Result := Copy(Lines[Index], Length(Prefix) + 1, Length(Lines[Index]) - Length(Prefix));
        Exit;
      end;
  finally
    Lines.Free;
  end;
end;

function PowerShellExecutable: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  if FileExists(Result) then
    Exit;
  Result := ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe');
  if FileExists(Result) then
    Exit;
  Result := '';
end;

// Every refusal keeps its cause and says what did not happen: no file of the target directory was
// written and no personal data was touched. Only codes the glue or the maintenance worker can
// return are named; anything else keeps the code visible for support.
function UpgradeRefusal(Code: String): String;
begin
  if Code = 'upgrade_running' then
    Result := RunningRefusal
  else if Code = 'upgrade_busy' then
    Result := '另一个升级维护操作正在进行，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请稍等片刻后重新运行本安装包。'
  else if Code = 'upgrade_worker_unsupported' then
    Result := '已安装的这一版没有配套的升级维护入口，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '升级必须由能读旧资料的那一版自己准备。请改用与该目录内版本配套的维护包；' + #13#10 +
      '在拿到之前，这个目录不要被覆盖：个人资料由两版程序共用，换个目录安装不能代替升级。'
  else if Code = 'upgrade_worker_missing' then
    Result := '目标程序缺少升级维护程序' + #13#10 +
      '（resources\storage\Qiban.Storage.exe），因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请先用与该目录内版本配套的安装包把它修复回原样，再重新运行本安装包。'
  else if Code = 'upgrade_carrier_missing' then
    Result := '这次升级记录的上一版程序副本不完整，因此没有覆盖任何文件。' + #13#10 + #13#10 +
      '请保留资料目录并人工检查，不要自行删除或覆盖程序目录。'
  else if Code = 'upgrade_target_mismatch' then
    Result := '这台电脑上有一个未完成的升级，它记录的目标程序不是本安装包，' + #13#10 +
      '因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请先用与该次升级配套的安装包把它做完，' + #13#10 +
      '或者用开始菜单里的“' + RecoveryLinkTitle + '”回到升级前的版本。'
  else if Code = 'upgrade_recovery_pending' then
    Result := '这台电脑正在等待回退上一次升级，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请先用开始菜单里的“' + RecoveryLinkTitle + '”完成或放弃那次回退，' + #13#10 +
      '再运行本安装包。'
  else if Code = 'upgrade_pending' then
    Result := '上一次升级还没有切换到新资料，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请关闭这个向导，重新运行这次升级所用的同一个安装包把它做完；' + #13#10 +
      '如果仍然停在这一步，请用开始菜单里的“' + RecoveryLinkTitle + '”检查升级状态。'
  else if (Code = 'installation_not_ready') or (Code = 'installation_incomplete') then
    Result := '这台电脑的默认资料目录里，安装还没有准备就绪，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请先正常打开一次栖伴并从窗口内退出，让它完成资料准备；' + #13#10 +
      '仍然不行时请保留资料目录并人工检查。'
  else if Code = 'recovery_link_failed' then
    Result := '无法创建“' + RecoveryLinkTitle + '”入口，程序文件因此没有被覆盖。' + #13#10 + #13#10 +
      '升级准备已经写下的记录会保留下来：请检查开始菜单目录的写入权限后，' + #13#10 +
      '重新运行这次升级所用的同一个安装包把它做完。'
  else if Code = 'upgrade_journal_invalid' then
    Result := '本机资料目录里的升级记录无法读取，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '没有删除、移动或改动任何资料。请保留资料目录并人工检查；' + #13#10 +
      '不要用重新安装或删除资料来绕过这一步。'
  else if (Code = 'target_manifest_invalid') or (Code = 'target_manifest_mismatch') or
          (Code = 'target_identity_invalid') or (Code = 'target_manifest_missing') then
    Result := '安装包自带的发布清单无法核对，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请重新获取与该目录内版本配套的安装包，不要继续使用这个文件。'
  else if (Code = 'upgrade_program_changed') or (Code = 'upgrade_program_invalid') or
          (Code = 'upgrade_app_missing') or (Code = 'app_identity_invalid') then
    Result := '目标目录里的程序文件与它自己的发布身份对不上，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请先用与该目录内版本配套的安装包把它修复回原样，再重新运行本安装包。'
  else if Code = 'upgrade_database' then
    Result := '本机数据库无法由升级准备流程使用，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请先回到栖伴窗口正常退出（不要强制结束进程），再重新运行本安装包。'
  else if Code = 'upgrade_vault' then
    Result := '本机凭据库不可用，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请保留资料目录并人工检查，不要重新安装或删除资料来绕过这一步。'
  else if Code = 'upgrade_directory_permissions' then
    Result := '无法在资料目录旁保存上一版程序副本，因此这次没有覆盖任何文件。' + #13#10 + #13#10 +
      '请检查磁盘空间与资料目录权限后重试。'
  else if Code = 'data_root_invalid' then
    Result := '安装器无法确定这次要使用的资料根目录，因此没有覆盖任何文件。' + #13#10 + #13#10 +
      '如果这次用了 /DataRoot= 参数，请检查它是不是一个绝对路径；' + #13#10 +
      '不带这个参数运行时会使用默认资料根。'
  else if (Code = 'powershell_missing') or (Code = 'powershell_incomplete') then
    Result := '这台电脑的 Windows PowerShell 无法完成这次升级准备，因此没有覆盖任何文件。' + #13#10 + #13#10 +
      '请改用与该目录内版本配套的维护包；不要为此重新安装或删除个人资料。'
  else
    Result := '这次配套升级没有通过准备步骤，程序文件因此没有被覆盖。' + #13#10 + #13#10 +
      '准备过程可能已经留下了升级记录（含上一次的资料保全与旧版程序副本），' + #13#10 +
      '它们不会被自动清理，也不会自动回退。' + #13#10 +
      '请重新运行这次升级所用的同一个安装包重试；' + #13#10 +
      '如果反复失败，请用开始菜单里的“' + RecoveryLinkTitle + '”检查升级状态' + #13#10 +
      '（代码：' + Code + '）。';
end;

// One glue run. It returns True only when the script reported "ready"; the code it left behind is
// what the person is told when it did not, so a refusal is never guessed from an exit code alone.
function RunUpgradeGlue(Mode, Target: String; var Branch, Code: String): Boolean;
var
  Script, Manifest, Parameters, Executable, Root, LinkPath: String;
  ExitCode: Integer;
begin
  Result := False;
  Branch := '';
  Code := '';
  Root := InstallationRootDirectory;
  if Root = '' then
  begin
    Code := 'data_root_invalid';
    Exit;
  end;
  Executable := PowerShellExecutable;
  if Executable = '' then
  begin
    Code := 'powershell_missing';
    Exit;
  end;
  // Both files are embedded with dontcopy and land in {tmp} under their own names. The manifest is
  // a byte-identical copy of the installed qiban-payload.json that build_installer.py writes under
  // a second, setup-only name, so extraction never has to pick between two identical entries.
  ExtractTemporaryFile(GlueScriptName);
  ExtractTemporaryFile(PayloadManifestName);
  Script := AddBackslash(ExpandConstant('{tmp}')) + GlueScriptName;
  Manifest := AddBackslash(ExpandConstant('{tmp}')) + PayloadManifestName;
  if (not FileExists(Script)) or (not FileExists(Manifest)) then
  begin
    Code := 'upgrade_glue_missing';
    Exit;
  end;
  LinkPath := RecoveryShortcutPath;
  DeleteFile(GlueResultPath);
  // One argument list, never a shell string: every value is a single quoted path and the identity
  // is a compiled-in digest. The target identity is passed lowercase, exactly as the backend's
  // own journal format requires.
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script + '"'
    + ' -Mode ' + Mode
    + ' -AppDir "' + Target + '"'
    + ' -InstallationRoot "' + Root + '"'
    + ' -TargetIdentity ' + ExpectedIdentitySha256
    + ' -TargetManifestPath "' + Manifest + '"'
    + ' -RecoveryLinkPath "' + LinkPath + '"'
    + ' -ResultPath "' + GlueResultPath + '"';
  if not Exec(Executable, Parameters, ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated,
              ExitCode) then
  begin
    Code := 'upgrade_glue_start_failed';
    Exit;
  end;
  Branch := ReadGlueValue('branch');
  Code := ReadGlueValue('code');
  Log('upgrade glue ' + Mode + ': exit=' + IntToStr(ExitCode) + ' branch=' + Branch +
      ' code=' + Code);
  Result := (ExitCode = 0) and (ReadGlueValue('status') = 'ready');
end;

// The prepare decision for one target directory. It never copies anything itself: the user's
// installed build prepares its own transaction, or this returns a refusal.
function PrepareUpgrade(Target: String): String;
var
  Branch, Code: String;
begin
  Result := '';
  UpgradeFinalizeNeeded := False;
  if RunUpgradeGlue('Prepare', Target, Branch, Code) then
  begin
    // A paired upgrade (or a repair that finishes an already switched transaction) is the only
    // shape whose target directory can still hold files only the previous build published.
    UpgradeFinalizeNeeded := (Branch = 'paired') or (Branch = 'resumed')
      or ((Branch = 'repair') and (ReadGlueValue('open') = '1'));
    Log('upgrade prepare: branch=' + Branch);
    Exit;
  end;
  if Code = '' then
    Code := 'upgrade_glue_failed';
  Result := UpgradeRefusal(Code);
end;

// The forward clean-up: after the new published files are in place, remove the files only the
// previous build published. It never touches anything else, never runs for a repair, and a
// failure here is only reported - the setup does not roll anything back by itself.
procedure UpgradeFinalize;
var
  Branch, Code: String;
begin
  if RunUpgradeGlue('Finalize', ExpandConstant('{app}'), Branch, Code) then
  begin
    Log('upgrade finalize: removed=' + ReadGlueValue('removed') +
        ' skipped=' + ReadGlueValue('skipped'));
    Exit;
  end;
  if Code = '' then
    Code := 'upgrade_glue_failed';
  Log('upgrade finalize did not finish: ' + Code);
  SuppressibleMsgBox(
    '程序已经装好了，但升级前那一版独有的旧文件没有全部清理干净' + #13#10 +
    '（代码：' + Code + '）。已有文件和资料都在，安装器没有自动回退。' + #13#10 + #13#10 +
    '可以重新运行本安装包再清理一次，或继续使用栖伴；' + #13#10 +
    '如果打开栖伴出现异常，请用开始菜单里的“' + RecoveryLinkTitle + '”' + #13#10 +
    '回到升级前的版本；不要手动删除程序目录。', mbInformation, MB_OK, IDOK);
end;

// Called on the "Preparing to Install" page: the last point before any file is written.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Target, RecordPath, ExePath, IdentityPath: String;
begin
  NeedsRestart := False;
  Result := '';
  if CheckForMutexes(RunningMutexName) then
  begin
    Result := RunningRefusal;
    Exit;
  end;
  Target := ExpandConstant('{app}');
  RecordPath := AddBackslash(Target) + BuildRecordName;
  ExePath := AddBackslash(Target) + AppExeName;
  IdentityPath := AddBackslash(Target) + PayloadIdentityName;
  if not FileExists(IdentityPath) then
  begin
    // An installation made by a package without this record cannot prove which build it is.
    // Its identity is never invented here; a new or empty directory stays installable.
    if FileExists(ExePath) or FileExists(RecordPath) then
      Result := MissingIdentityRefusal;
    Exit;
  end;
  // Equal digests mean this package ships exactly that published file set: every file, including
  // notes, licenses and every component resource. The target's own files are not compared - a
  // damaged or missing payload file is what a repair is for.
  if CompareText(GetSHA256OfFile(IdentityPath), ExpectedIdentitySha256) <> 0 then
  begin
    // Another build: only the installed build itself may pair a new program with this machine's
    // data. The glue asks that build's own frozen worker to prepare the transaction and refuses
    // to overwrite anything when it cannot. Setup writes no file before this returns ''.
    Result := PrepareUpgrade(Target);
    Exit;
  end;
  if FileExists(RecordPath) then
  begin
    // The shell record already names this executable and its desktop sources; keep that rule.
    if CompareText(GetSHA256OfFile(RecordPath), ExpectedRecordSha256) <> 0 then
    begin
      Result := RecordRefusal;
      Exit;
    end;
  end
  else if FileExists(ExePath) then
  begin
    Result := UnrecordedRefusal;
    Exit;
  end;
  // Same build: a repair. It copies the same published file set again and never creates or
  // replaces a clone; the glue still has to confirm that no unfinished transaction points at
  // another target, and it keeps (or rebuilds) the recorded way back.
  Result := PrepareUpgrade(Target);
end;

// The file copy is done and the target directory now publishes this build. Only a paired upgrade
// has files that the previous build published and this one does not; every other shape ends here.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;
  if not UpgradeFinalizeNeeded then
    Exit;
  UpgradeFinalize;
end;

function InitializeUninstall: Boolean;
var
  Application: String;
  ResultCode: Integer;
begin
  Result := True;
  // A silent uninstall keeps exactly its previous rules: it never opens the interactive preparation
  // page and never waits on a dialog, and it still refuses while the application is running.
  if UninstallSilent then
  begin
    if CheckForMutexes(RunningMutexName) then
    begin
      SuppressibleMsgBox(RunningRefusal + #13#10 + #13#10 +
        '卸载没有开始，程序和资料都保持原样。', mbError, MB_OK, IDOK);
      Result := False;
    end;
    Exit;
  end;
  // Only the application's own preparation page may continue into the real uninstallation: it
  // starts this same uninstaller with the one fixed internal parameter, and only after the person
  // made an explicit choice there and the application (with its database) was closed normally.
  if CompareText(ExpandConstant('{param:' + PreparedParameter + '|}'), '1') = 0 then
  begin
    if CheckForMutexes(RunningMutexName) then
    begin
      SuppressibleMsgBox(RunningRefusal + #13#10 + #13#10 +
        '卸载没有开始，程序和资料都保持原样。', mbError, MB_OK, IDOK);
      Result := False;
    end;
    Exit;
  end;
  // An ordinary interactive uninstall (Start Menu entry or the Windows application list) first
  // opens the preparation page and then stops here. The page decides about the backup, closes the
  // application and comes back with the parameter above; it is never started in a loop from here,
  // because this branch only runs for a run that did not carry that parameter.
  Application := AddBackslash(ExpandConstant('{app}')) + AppExeName;
  if FileExists(Application) then
  begin
    if not Exec(Application, PreparationSwitch, ExpandConstant('{app}'), SW_SHOWNORMAL, ewNoWait,
                ResultCode) then
      SuppressibleMsgBox(
        '无法打开栖伴的卸载准备页，因此这次没有开始卸载。' + #13#10 + #13#10 +
        '程序、个人资料、备份与凭据都保持原样。请重新运行本卸载程序再试一次。',
        mbError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;
  // The program directory is already damaged. Say what can no longer happen and keep the ordinary
  // uninstallation: it only removes the recorded program files and shortcuts.
  SuppressibleMsgBox(
    '程序目录里没有可用于准备页的栖伴主程序，因此这次无法先备份。' + #13#10 + #13#10 +
    '卸载将继续，只删除安装器登记的程序文件和快捷方式；个人资料、备份与凭据仍然保留。',
    mbInformation, MB_OK, IDOK);
end;
