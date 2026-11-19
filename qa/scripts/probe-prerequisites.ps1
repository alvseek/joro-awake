#requires -Version 5.1
<#
.SYNOPSIS
Read-only inventory of the inputs the Joro Awake packaged Windows installer boundary needs.

.DESCRIPTION
This probe answers one narrow question: what does this machine actually have right
now, and which observed inputs are missing? It is an INVENTORY, not a readiness
verdict. A filesystem and version-command observation cannot establish account
access, hook trust, test isolation or Store authority, so the report always says
`readiness.status = not_established` and lists the inputs only a person can supply.

Observations are kept separate from operator declarations:
  - `client.installed`, `client.version_status` and `client.config_state` are observed.
  - A declaration is echoed from an environment variable with `declared = true`,
    `verified = false`, and never turns into evidence of access.

Nothing is created, installed, started or configured. The only write the probe can
make is `-OutFile`. Without it the probe changes no file and no process.

Parameters exist so a test can point the probe at a scratch root instead of the
live profile:
  -HomeRoot     where `~/.claude`, `~/.codex`, `~/.cursor`, `~/.copilot`, `~/.gemini` live
  -AppDataRoot  where `Joro\keep-awake*` and per-user program directories live

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File qa/scripts/probe-prerequisites.ps1

.EXAMPLE
# also write the report; this is the probe's only write
powershell -NoProfile -ExecutionPolicy Bypass -File qa/scripts/probe-prerequisites.ps1 -OutFile .\inventory.json
#>
[CmdletBinding()]
param(
    [string]$HomeRoot = $HOME,
    [string]$AppDataRoot = $env:LOCALAPPDATA,
    [int]$CommandTimeoutMs = 15000,
    [string]$OutFile
)

$ErrorActionPreference = 'Stop'

function Get-CommandPath {
    param([string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Stop-ProcessTree {
    param([int]$TargetPid)
    try {
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$TargetPid" -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-ProcessTree -TargetPid $_.ProcessId }
    } catch { }
    try { Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue } catch { }
}

function Invoke-BoundedCommand {
    # A hung version command must not hang the probe, so the call runs in a child
    # PowerShell with a hard timeout and its process tree is killed on expiry.
    param(
        [string]$File,
        [string[]]$Arguments = @(),
        [int]$TimeoutMs = 15000
    )
    $quote = { param($value) "'" + ([string]$value -replace "'", "''") + "'" }
    $line = '& ' + (& $quote $File)
    if ($Arguments.Count -gt 0) {
        $line += ' ' + (($Arguments | ForEach-Object { & $quote $_ }) -join ' ')
    }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($line))
    $shell = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $shell)) { $shell = 'powershell.exe' }

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $shell
    $startInfo.Arguments = "-NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = $null
    try {
        $process = [System.Diagnostics.Process]::Start($startInfo)
    } catch {
        return [pscustomobject]@{ status = 'error'; exit_code = $null; output = $null; error = $_.Exception.Message }
    }
    try {
        $outTask = $process.StandardOutput.ReadToEndAsync()
        $errTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMs)) {
            Stop-ProcessTree -TargetPid $process.Id
            return [pscustomobject]@{ status = 'timeout'; exit_code = $null; output = $null; error = "no answer within $TimeoutMs ms" }
        }
        $out = $outTask.Result
        $err = $errTask.Result
        return [pscustomobject]@{ status = 'ok'; exit_code = $process.ExitCode; output = $out; error = $err }
    } finally {
        if ($process) { $process.Dispose() }
    }
}

function ConvertTo-Version {
    param([string]$Text)
    if (-not $Text) { return $null }
    $match = [regex]::Match($Text, '\d+\.\d+(\.\d+)?')
    if ($match.Success) { return $match.Value }
    return $null
}

function Get-PathObservation {
    # state: present | missing | unreadable. A directory is never hashed.
    param([string]$Path, [string]$Kind = 'file')
    if (-not $Path) {
        return [pscustomobject]@{ path = $null; state = 'unspecified'; sha256 = $null }
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ path = $Path; state = 'missing'; sha256 = $null }
    }
    if ($Kind -eq 'dir') {
        return [pscustomobject]@{ path = $Path; state = 'present'; sha256 = $null }
    }
    try {
        $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
        return [pscustomobject]@{ path = $Path; state = 'present'; sha256 = $hash }
    } catch {
        return [pscustomobject]@{ path = $Path; state = 'unreadable'; sha256 = $null }
    }
}

function Get-VersionObservation {
    # Installed evidence and version evidence are resolved separately: a command
    # that reports nothing is a qualified result, never a silent success.
    param(
        [string]$CommandPath,
        [string[]]$Arguments = @('--version'),
        [int]$TimeoutMs = 15000
    )
    if (-not $CommandPath) {
        return [pscustomobject]@{ status = 'unavailable'; version = $null; raw = $null; detail = 'no command on PATH' }
    }
    $result = Invoke-BoundedCommand -File $CommandPath -Arguments $Arguments -TimeoutMs $TimeoutMs
    if ($result.status -eq 'timeout') {
        return [pscustomobject]@{ status = 'timeout'; version = $null; raw = $null; detail = $result.error }
    }
    if ($result.status -eq 'error') {
        return [pscustomobject]@{ status = 'error'; version = $null; raw = $null; detail = $result.error }
    }
    $first = (([string]$result.output) -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    if (-not $first) {
        return [pscustomobject]@{ status = 'not_reported'; version = $null; raw = $null; detail = 'command produced no output' }
    }
    $version = ConvertTo-Version $first
    if (-not $version) {
        return [pscustomobject]@{ status = 'unreported_format'; version = $null; raw = $first.Trim(); detail = 'no version number in the first line' }
    }
    return [pscustomobject]@{ status = 'reported'; version = $version; raw = $first.Trim(); detail = $null }
}

function Get-FileVersionObservation {
    # A desktop app reports its version through the executable's metadata, so a
    # missing CLI is not a missing version.
    param([string]$Path)
    if (-not $Path) {
        return [pscustomobject]@{ status = 'unavailable'; version = $null; raw = $null; detail = 'no executable' }
    }
    try {
        $info = (Get-Item -LiteralPath $Path).VersionInfo
        $raw = $info.ProductVersion
        if (-not $raw) { $raw = $info.FileVersion }
        $version = ConvertTo-Version $raw
        if ($version) {
            return [pscustomobject]@{ status = 'reported'; version = $version; raw = [string]$raw; detail = $null }
        }
        return [pscustomobject]@{ status = 'not_reported'; version = $null; raw = [string]$raw; detail = 'no version in the executable metadata' }
    } catch {
        return [pscustomobject]@{ status = 'error'; version = $null; raw = $null; detail = $_.Exception.Message }
    }
}

function New-UnavailableVersion {
    param([string]$Detail = 'not queried')
    return [pscustomobject]@{ status = 'unavailable'; version = $null; raw = $null; detail = $Detail }
}

function Get-ClientStatus {
    param(
        [bool]$Installed,
        [string]$VersionStatus,
        [string]$ConfigState
    )
    if (-not $Installed) { return 'absent' }
    $configuration = switch ($ConfigState) {
        'present' { 'configured' }
        'missing' { 'unconfigured' }
        'unreadable' { 'config_unreadable' }
        default { 'config_unspecified' }
    }
    $version = if ($VersionStatus -eq 'reported') { 'version_known' } else { 'version_unverified' }
    return "present_${configuration}_${version}"
}

function New-ClientObservation {
    param(
        [string]$Name,
        [string]$Surface,
        [bool]$Installed,
        [string[]]$InstallEvidence = @(),
        [string]$ConfigLabel,
        [string]$ConfigPath,
        [string]$ConfigState = 'unspecified',
        [string]$VersionStatus = 'unavailable',
        [string]$Version,
        [string]$VersionRaw
    )
    $status = Get-ClientStatus -Installed $Installed -VersionStatus $VersionStatus -ConfigState $ConfigState
    $blocking = @()
    if (-not $Installed) { $blocking += "client: $Name not installed" }
    if ($Installed -and $VersionStatus -ne 'reported') { $blocking += "version: $Name $VersionStatus" }
    if ($Installed -and $ConfigState -eq 'unreadable') { $blocking += "config: $Name unreadable" }
    return [pscustomobject]@{
        name             = $Name
        surface          = $Surface
        installed        = $Installed
        install_evidence = @($InstallEvidence)
        version          = $Version
        version_status   = $VersionStatus
        version_raw      = $VersionRaw
        config_label     = $ConfigLabel
        config_path      = $ConfigPath
        config_state     = $ConfigState
        status           = $status
        blocking         = @($blocking)
    }
}

function Add-ClientRow {
    param(
        [System.Collections.ArrayList]$List,
        [string]$Name,
        [string]$Surface,
        [bool]$Installed,
        [string[]]$InstallEvidence,
        [string]$ConfigLabel,
        [string]$ConfigPath,
        [string]$ConfigKind = 'file',
        [string]$CommandPath,
        [string[]]$VersionArguments = @('--version'),
        [object]$VersionObservation
    )
    $config = Get-PathObservation -Path $ConfigPath -Kind $ConfigKind
    if ($VersionObservation) {
        $version = $VersionObservation
    } else {
        $version = Get-VersionObservation -CommandPath $CommandPath -Arguments $VersionArguments -TimeoutMs $CommandTimeoutMs
    }
    $row = New-ClientObservation -Name $Name -Surface $Surface -Installed $Installed `
        -InstallEvidence $InstallEvidence -ConfigLabel $ConfigLabel -ConfigPath $ConfigPath `
        -ConfigState $config.state -VersionStatus $version.status -Version $version.version -VersionRaw $version.raw
    [void]$List.Add($row)
    return $row
}

function Get-LiveJoro {
    param([string]$AppDataRoot, [string]$HomeRoot)
    $stateRoot = Join-Path $AppDataRoot 'Joro\keep-awake-state'
    $installRoot = Join-Path $AppDataRoot 'Joro\keep-awake'
    $live = [ordered]@{
        state_dir         = $stateRoot
        install_dir       = $installRoot
        installed_version = $null
        helper            = $null
        tray              = $null
        startup_value     = $null
    }

    $versionFile = Join-Path $installRoot 'VERSION'
    if (Test-Path -LiteralPath $versionFile) {
        $live.installed_version = (Get-Content -LiteralPath $versionFile -Raw).Trim()
    }

    foreach ($pair in @(@('helper', 'helper.json'), @('tray', 'tray.json'))) {
        $file = Join-Path $stateRoot $pair[1]
        if (-not (Test-Path -LiteralPath $file)) { continue }
        try {
            $value = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
            $live[$pair[0]] = [ordered]@{
                pid             = $value.pid
                birth           = $value.birth
                updated_at      = $value.updated_at
                power_requested = $value.power_requested
                active_count    = $value.active_count
            }
        } catch {
            $live[$pair[0]] = [ordered]@{ unreadable = $true }
        }
    }

    $live.startup_value = $null
    try {
        $runValue = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'JoroKeepAwake' -ErrorAction Stop
        $live.startup_value = $runValue.JoroKeepAwake
    } catch { }

    return $live
}

function Invoke-Probe {
    $hostInfo = [ordered]@{
        os_caption     = $null
        os_version     = $null
        os_build       = $null
        os_ubr         = $null
        display_version = $null
        edition        = $null
        architecture   = $env:PROCESSOR_ARCHITECTURE
        is_64bit_os    = [Environment]::Is64BitOperatingSystem
        user           = $env:USERNAME
        dotnet_sdks    = @()
    }
    try {
        $os = Get-CimInstance Win32_OperatingSystem
        $hostInfo.os_caption = $os.Caption
        $hostInfo.os_version = $os.Version
        $hostInfo.os_build = [string]$os.BuildNumber
    } catch { }
    $currentVersion = Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
    if ($currentVersion) {
        $hostInfo.os_ubr = $currentVersion.UBR
        $hostInfo.display_version = $currentVersion.DisplayVersion
        $hostInfo.edition = $currentVersion.EditionID
    }
    $dotnet = Get-CommandPath 'dotnet'
    if ($dotnet) {
        $sdkResult = Invoke-BoundedCommand -File $dotnet -Arguments @('--list-sdks') -TimeoutMs $CommandTimeoutMs
        if ($sdkResult.status -eq 'ok' -and $sdkResult.output) {
            $hostInfo.dotnet_sdks = @(([string]$sdkResult.output) -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() })
        }
    }

    # --- build and certification tools -------------------------------------

    $kitRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10'
    $kitIncludes = @()
    if (Test-Path -LiteralPath (Join-Path $kitRoot 'Include')) {
        $kitIncludes = @(Get-ChildItem -LiteralPath (Join-Path $kitRoot 'Include') -Directory | Select-Object -ExpandProperty Name)
    }

    function Find-KitTool {
        param([string]$Name, [string]$SearchRoot, [string]$Arch = 'x64')
        $onPath = Get-CommandPath $Name
        if ($onPath) { return $onPath }
        if (-not $SearchRoot -or -not (Test-Path -LiteralPath $SearchRoot)) { return $null }
        if ($Arch) {
            $preferred = Get-ChildItem -LiteralPath $SearchRoot -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName (Join-Path $Arch $Name) } |
                Where-Object { Test-Path -LiteralPath $_ } |
                Sort-Object -Descending | Select-Object -First 1
            if ($preferred) { return $preferred }
        }
        $found = Get-ChildItem -LiteralPath $SearchRoot -Recurse -Filter $Name -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
        return $null
    }

    $makeappx = Find-KitTool -Name 'makeappx.exe' -SearchRoot (Join-Path $kitRoot 'bin')
    $signtool = Find-KitTool -Name 'signtool.exe' -SearchRoot (Join-Path $kitRoot 'bin')
    $appcert = Find-KitTool -Name 'appcert.exe' -SearchRoot (Join-Path $kitRoot 'App Certification Kit') -Arch ''

    $buildTools = [ordered]@{
        kit_root             = if (Test-Path -LiteralPath $kitRoot) { $kitRoot } else { $null }
        sdk_include_versions = $kitIncludes
        makeappx             = $makeappx
        signtool             = $signtool
        appcert              = $appcert
        satisfied            = [bool]($makeappx -and $signtool -and $appcert)
    }

    # --- client surfaces ----------------------------------------------------

    $clients = New-Object System.Collections.ArrayList
    $pendingConfiguration = @()

    $claudePath = Get-CommandPath 'claude'
    Add-ClientRow -List $clients -Name 'Claude Code' -Surface 'local CLI' `
        -Installed ([bool]$claudePath) -InstallEvidence @(if ($claudePath) { "command: $claudePath" } else { 'no claude command' }) `
        -ConfigLabel 'hooks file ~/.claude/settings.json' -ConfigPath (Join-Path $HomeRoot '.claude\settings.json') `
        -CommandPath $claudePath | Out-Null

    $opencodePath = Get-CommandPath 'opencode'
    Add-ClientRow -List $clients -Name 'OpenCode' -Surface 'local CLI' `
        -Installed ([bool]$opencodePath) -InstallEvidence @(if ($opencodePath) { "command: $opencodePath" } else { 'no opencode command' }) `
        -ConfigLabel 'plugin dir ~/.config/opencode/plugins/joro-keep-awake' `
        -ConfigPath (Join-Path $HomeRoot '.config\opencode\plugins\joro-keep-awake') -ConfigKind 'dir' `
        -CommandPath $opencodePath | Out-Null

    $codexPath = Get-CommandPath 'codex'
    Add-ClientRow -List $clients -Name 'Codex' -Surface 'local CLI' `
        -Installed ([bool]$codexPath) -InstallEvidence @(if ($codexPath) { "command: $codexPath" } else { 'no codex command' }) `
        -ConfigLabel 'hook file ~/.codex/hooks.json' -ConfigPath (Join-Path $HomeRoot '.codex\hooks.json') `
        -CommandPath $codexPath | Out-Null

    # Cursor's surface is the desktop Agent, so the app is the installation
    # evidence; a CLI on PATH is neither required nor sufficient for it.
    $cursorCandidates = @(
        (Join-Path $env:ProgramFiles 'Cursor\Cursor.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Cursor\Cursor.exe'),
        (Join-Path $AppDataRoot 'Programs\cursor\Cursor.exe'),
        (Join-Path $AppDataRoot 'Programs\Cursor\Cursor.exe')
    )
    $cursorApp = $null
    foreach ($candidate in $cursorCandidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $cursorApp = $candidate; break }
    }
    $cursorCli = Get-CommandPath 'cursor'
    $cursorEvidence = @()
    if ($cursorApp) { $cursorEvidence += "app: $cursorApp" }
    if ($cursorCli) { $cursorEvidence += "cli: $cursorCli" }
    if (-not $cursorApp -and -not $cursorCli) { $cursorEvidence += 'no Cursor app or command' }
    $cursorVersion = if ($cursorCli) {
        Get-VersionObservation -CommandPath $cursorCli -Arguments @('--version') -TimeoutMs $CommandTimeoutMs
    } elseif ($cursorApp) {
        Get-FileVersionObservation -Path $cursorApp
    } else {
        New-UnavailableVersion -Detail 'Cursor is not installed'
    }
    Add-ClientRow -List $clients -Name 'Cursor' -Surface 'desktop Agent' `
        -Installed ([bool]($cursorApp -or $cursorCli)) -InstallEvidence $cursorEvidence `
        -ConfigLabel 'hook file ~/.cursor/hooks.json' -ConfigPath (Join-Path $HomeRoot '.cursor\hooks.json') `
        -CommandPath $cursorCli -VersionObservation $cursorVersion | Out-Null

    $codePath = Get-CommandPath 'code'
    $copilotExtension = $null
    $vscodeVersion = $null
    $extensionStatus = 'not_queried'
    if ($codePath) {
        $versionResult = Invoke-BoundedCommand -File $codePath -Arguments @('--version') -TimeoutMs $CommandTimeoutMs
        if ($versionResult.status -eq 'ok' -and $versionResult.output) {
            $vscodeVersion = ConvertTo-Version (([string]$versionResult.output) -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
        }
        $listResult = Invoke-BoundedCommand -File $codePath -Arguments @('--list-extensions', '--show-versions') -TimeoutMs $CommandTimeoutMs
        if ($listResult.status -eq 'timeout') { $extensionStatus = 'query_timeout' }
        elseif ($listResult.status -eq 'error') { $extensionStatus = 'query_error' }
        else {
            $extensionStatus = 'queried'
            $copilotExtension = (([string]$listResult.output) -split "`r?`n" |
                Where-Object { $_ -match '(?i)copilot' } | Select-Object -First 1)
        }
    } else { $extensionStatus = 'no_vscode_command' }
    $copilotEvidence = @()
    if ($codePath) { $copilotEvidence += "vscode: $codePath (version $vscodeVersion)" } else { $copilotEvidence += 'no code command' }
    if ($copilotExtension) { $copilotEvidence += "extension: $copilotExtension" }
    else { $copilotEvidence += "extension: none reported ($extensionStatus)" }
    $staleCopilot = Get-ChildItem -LiteralPath (Join-Path $HomeRoot '.vscode\extensions') -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'github.copilot*' } | Select-Object -First 1
    if ($staleCopilot -and -not $copilotExtension) {
        $copilotEvidence += "stale folder $($staleCopilot.Name) is not in the extension manifest"
    }
    $copilotVersion = if ($copilotExtension) {
        [pscustomobject]@{ status = 'reported'; version = (ConvertTo-Version $copilotExtension); raw = $copilotExtension; detail = $null }
    } else {
        New-UnavailableVersion -Detail 'no Copilot extension reported'
    }
    $copilotInstalled = [bool]($codePath -and $copilotExtension)
    Add-ClientRow -List $clients -Name 'GitHub Copilot in VS Code' -Surface 'VS Code agent' `
        -Installed $copilotInstalled -InstallEvidence $copilotEvidence `
        -ConfigLabel 'hook dir ~/.copilot/hooks' -ConfigPath (Join-Path $HomeRoot '.copilot\hooks') -ConfigKind 'dir' `
        -CommandPath $codePath -VersionObservation $copilotVersion | Out-Null

    $antigravityApp = $null
    foreach ($candidate in @((Join-Path $AppDataRoot 'Programs\Antigravity\Antigravity.exe'),
                            (Join-Path $AppDataRoot 'Programs\Antigravity IDE\Antigravity IDE.exe'))) {
        if (Test-Path -LiteralPath $candidate) { $antigravityApp = $candidate; break }
    }
    $antigravityEvidence = @()
    if ($antigravityApp) { $antigravityEvidence += "app: $antigravityApp" } else { $antigravityEvidence += 'no Antigravity App/IDE' }
    $antigravityVersion = if ($antigravityApp) { Get-FileVersionObservation -Path $antigravityApp } else { New-UnavailableVersion -Detail 'Antigravity is not installed' }
    Add-ClientRow -List $clients -Name 'Antigravity' -Surface 'local App/IDE' `
        -Installed ([bool]$antigravityApp) -InstallEvidence $antigravityEvidence `
        -ConfigLabel 'global hooks ~/.gemini/config/hooks.json' -ConfigPath (Join-Path $HomeRoot '.gemini\config\hooks.json') `
        -CommandPath $null -VersionObservation $antigravityVersion | Out-Null

    # An installed client with no hook file is the expected first-run state, not
    # a missing application. Report it separately from a blocker.
    foreach ($client in $clients) {
        if ($client.installed -and $client.config_state -eq 'missing') {
            $pendingConfiguration += "client: $($client.name) installed, hooks not configured yet"
        }
    }

    # --- inventory roll-up --------------------------------------------------

    $blocking = @()
    foreach ($client in $clients) { $blocking += @($client.blocking) }
    if (-not $buildTools.satisfied) { $blocking += 'build tools: makeappx/signtool/appcert' }

    $inventory = [ordered]@{
        required_clients   = $clients.Count
        installed_clients  = @($clients | Where-Object { $_.installed }).Count
        blocking           = @($blocking)
        pending_configuration = @($pendingConfiguration)
    }

    # --- declarations and the readiness line --------------------------------

    $arm64Declaration = $env:JORO_QA_ARM64_TARGET
    $storeDeclaration = $env:JORO_QA_STORE_IDENTITY_REF

    # Nothing here can be verified by a filesystem or version check, and a
    # declaration is not verification. The gate each one blocks is named so the
    # later Store identity is not pulled forward into Step 1.2.
    $unverified = @(
        [ordered]@{ item = 'native ARM64 test target'; blocks = 'Step 1.2 and later native evidence'; how = 'set JORO_QA_ARM64_TARGET to a device/VM reference'; declared = [bool]$arm64Declaration; declaration = $arm64Declaration; verified = $false },
        [ordered]@{ item = 'Cursor desktop app with a test account'; blocks = 'Step 1.2 client adaptation'; how = 'install Cursor and sign in on a disposable boundary'; declared = $false; declaration = $null; verified = $false },
        [ordered]@{ item = 'GitHub Copilot subscription, install and sign-in'; blocks = 'Step 1.2 client adaptation'; how = 'install the Copilot extension and sign in on a disposable boundary'; declared = $false; declaration = $null; verified = $false },
        [ordered]@{ item = 'disposable Codex home and login'; blocks = 'Step 1.2 client adaptation'; how = 'provide a scratch CODEX_HOME and test login'; declared = $false; declaration = $null; verified = $false },
        [ordered]@{ item = 'separate Windows user for Antigravity'; blocks = 'Step 1.2 client adaptation'; how = 'create a disposable Windows user or grant scoped test consent'; declared = $false; declaration = $null; verified = $false },
        [ordered]@{ item = 'development package identity and test certificate'; blocks = 'Step 1.2 packaged probe'; how = 'Step 1.2 creates and installs it on the disposable boundary only'; declared = $false; declaration = $null; verified = $false },
        [ordered]@{ item = 'reserved Store identity from the owner account'; blocks = 'Step 3.2 production manifest and Step 6.2 submission'; how = 'owner supplies nonsecret Name/Publisher/PublisherDisplayName later; not needed for a development-identity probe'; declared = [bool]$storeDeclaration; declaration = $storeDeclaration; verified = $false }
    )

    $readiness = [ordered]@{
        status = 'not_established'
        reason = 'This probe observes files, commands and OS facts. It cannot establish account access, hook trust, disposable test isolation or Store authority, so it never declares the boundary ready.'
        unverified_inputs = $unverified
    }

    # --- live installation fingerprint --------------------------------------

    $live = Get-LiveJoro -AppDataRoot $AppDataRoot -HomeRoot $HomeRoot
    $fingerprint = [ordered]@{
        installed_version = $live.installed_version
        helper_pid        = if ($live.helper) { $live.helper.pid } else { $null }
        helper_birth      = if ($live.helper) { $live.helper.birth } else { $null }
        tray_pid          = if ($live.tray) { $live.tray.pid } else { $null }
        tray_birth        = if ($live.tray) { $live.tray.birth } else { $null }
        startup_value     = $live.startup_value
        files             = [ordered]@{
            claude_settings = Get-PathObservation -Path (Join-Path $HomeRoot '.claude\settings.json')
            opencode_config = Get-PathObservation -Path (Join-Path $HomeRoot '.config\opencode\opencode.jsonc')
            opencode_plugin = Get-PathObservation -Path (Join-Path $HomeRoot '.config\opencode\plugins\joro-keep-awake\index.js')
            codex_config    = Get-PathObservation -Path (Join-Path $HomeRoot '.codex\config.toml')
        }
    }

    $inventoryComplete = ($blocking.Count -eq 0)

    $writes = @()
    if ($OutFile) { $writes = @("report file: $OutFile") }

    $report = [ordered]@{
        probe              = 'joro-awake packaged Windows installer prerequisite inventory'
        mode               = 'inventory_only'
        writes             = $writes
        generated_at       = (Get-Date).ToString('o')
        timeout_ms         = $CommandTimeoutMs
        host               = $hostInfo
        clients            = @($clients)
        build_tools        = $buildTools
        inventory          = $inventory
        inventory_complete = $inventoryComplete
        readiness          = $readiness
        live_install       = $live
        fingerprint        = $fingerprint
    }

    $json = $report | ConvertTo-Json -Depth 8
    if ($OutFile) { Set-Content -LiteralPath $OutFile -Value $json -Encoding UTF8 }
    Write-Output $json
    if ($inventoryComplete) { exit 0 } else { exit 1 }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-Probe }
