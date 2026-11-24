#requires -Version 5.1
[CmdletBinding()]
param([string]$EvidenceDirectory, [string]$EndpointFile)

$ErrorActionPreference = 'Stop'
$client = Split-Path $PSScriptRoot -Parent
$repo = (Get-Item -LiteralPath $client).Parent.Parent.FullName
if (-not $EvidenceDirectory) {
    $EvidenceDirectory = Join-Path $repo ('.dev/remote-display-' + [Guid]::NewGuid().ToString('N'))
}
[IO.Directory]::CreateDirectory($EvidenceDirectory) | Out-Null
$EvidenceDirectory = (Resolve-Path -LiteralPath $EvidenceDirectory).Path
if (Test-Path -LiteralPath (Join-Path $EvidenceDirectory 'display.json')) {
    throw '请选择新的验收目录。'
}

$exe = Join-Path $client 'build/Qiban.Remote.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw '请先执行 windows-remote/build.ps1。' }
Add-Type -AssemblyName System.Windows.Forms,System.Drawing,System.Web.Extensions
[Windows.Forms.Application]::EnableVisualStyles()
$assembly = [Reflection.Assembly]::Load([IO.File]::ReadAllBytes($exe))
$options = [Collections.Generic.Dictionary[string,string]]::new()
$windowType = $assembly.GetType('Qiban.Remote.RemoteWindow', $true)
$window = [Activator]::CreateInstance($windowType, [Reflection.BindingFlags]'Instance, Public, NonPublic',
    $null, [object[]](,$options), $null)
$json = [Web.Script.Serialization.JavaScriptSerializer]::new()
$checks = [Collections.Generic.List[string]]::new()
$syntheticCount = 0
$passed = $false
$failure = $null
$live = [ordered]@{}

function Read-Value([string]$Name) {
    $found = $window.Controls.Find($Name, $true)
    if ($found.Length -ne 1) { throw "未找到唯一控件 $Name" }
    return $found[0].Text
}
function Check-Text([string]$Name, [string]$Expected) {
    $actual = Read-Value $Name
    if ($actual -cne $Expected) { throw "$Name — 期望「$Expected」，实际「$actual」。" }
    $checks.Add("$Name = $actual")
}
function Check-Dialogue([string]$Expected) {
    $title = $window.Controls.Find('SyntheticDialogueTitle', $true)[0]
    $value = $window.Controls.Find('SyntheticDialogue', $true)[0]
    $visible = $Expected.Length -gt 0
    $table = $title.Parent
    $table.PerformLayout()
    $height = $table.GetRowHeights()[$table.GetRow($title)]
    if ($title.Text -cne '测试会话' -or $title.Visible -ne $visible -or
        $value.Visible -ne $visible -or (($height -gt 0) -ne $visible)) {
        throw "测试会话行显示或收起状态错误（预期可见 $visible，实际行高 $height）。"
    }
    Check-Text SyntheticDialogue $Expected
}
function Save-View([string]$Name) {
    [Windows.Forms.Application]::DoEvents()
    $bmp = [Drawing.Bitmap]::new($view.Width, $view.Height)
    try {
        $view.DrawToBitmap($bmp, [Drawing.Rectangle]::new(0, 0, $view.Width, $view.Height))
        $bmp.Save((Join-Path $EvidenceDirectory $Name), [Drawing.Imaging.ImageFormat]::Png)
    } finally { $bmp.Dispose() }
}

try {
    $window.ShowInTaskbar = $false
    $window.Opacity = 0
    $window.Show()
    $view = $window.Controls.Find('StatusView', $true)[0]
    if (-not $view.IsHandleCreated) { throw '正常窗体未承载状态视图。' }
    Check-Text ConnectionState '已断开'
    Check-Text SampleState '尚无采样'
    Check-Text Completed '—'
    Check-Dialogue ''
    $view.SetConnection('connecting', '正在验证服务器身份')
    Check-Text ConnectionState '正在连接'
    $view.SetConnection('connected', '已验证服务器身份')
    Check-Text SampleState '尚无采样'

    # Exercise the same dictionary types the wire decoder supplies.
    $sample = $json.DeserializeObject('{"installation_id":"11111111111111111111111111111111","runtime_instance":"22222222222222222222222222222222","generated_at":"2026-11-24T09:10:11.123456Z","status":"running","heartbeat_count":24,"progress":{"queued":0,"running":0,"completed":0,"failed":0,"last_completed_id":null},"platforms":{"local":{"available":false,"status":"unavailable","reason":"未配置适配器"},"qq":{"available":false,"status":"unavailable","reason":"尚未接入"},"wechat":{"available":false,"status":"unavailable","reason":"尚未接入"},"bilibili":{"available":false,"status":"unavailable","reason":"尚未接入"}}}')
    $view.SetStatus($sample)
    Check-Text GeneratedAt '2026-11-24 09:10:11.123456 UTC'
    Check-Text Completed '0'
    Check-Text LastCompletedId '暂无完成记录'
    Check-Text PlatformQq '不可用 · 尚未接入'
    Check-Dialogue ''
    $sample['synthetic_dialogue'] = $json.DeserializeObject('{"backend":"postgresql","mode":"synthetic","confirmed":1,"uncertain":2,"submissions":3,"denied":4,"replays":0}')
    $view.SetStatus($sample)
    $expectedDialogue = '已确认 1 · 回执不明 2 · 实际提交 3 · 拒绝 4 · 重复 0'
    Check-Dialogue $expectedDialogue
    Check-Text Completed '0'
    Check-Text PlatformQq '不可用 · 尚未接入'
    $view.SetConnection('disconnected', '管理连接已断开')
    Check-Text SampleState '离线 · 上次采样'
    Check-Dialogue $expectedDialogue
    $sample.progress.completed = 1
    $sample.progress.last_completed_id = 'document-index-01'
    $sample.heartbeat_count = 35
    $sample.generated_at = '2026-11-24T09:10:21Z'
    $view.SetStatus($sample)
    Check-Text Completed '0'
    Check-Text HeartbeatCount '24'
    Check-Text GeneratedAt '2026-11-24 09:10:11.123456 UTC'
    $view.SetConnection('reconnecting', '正在重新验证')
    Check-Text ConnectionState '正在重连'
    Check-Text SampleState '离线 · 上次采样'
    $view.SetConnection('connected', '已验证服务器身份')
    Check-Text SampleState '等待新采样 · 显示上次采样'
    $view.SetStatus($sample)
    Check-Text SampleState '已采样'
    Check-Text HeartbeatCount '35'
    Check-Text Completed '1'
    Check-Text LastCompletedId 'document-index-01'
    Check-Text GeneratedAt '2026-11-24 09:10:21 UTC'
    [void]$sample.synthetic_dialogue.Remove('uncertain')
    $view.SetStatus($sample)
    Check-Dialogue '已确认 1 · 回执不明 未提供 · 实际提交 3 · 拒绝 4 · 重复 0'
    $sample.synthetic_dialogue.mode = 'unknown'
    $view.SetStatus($sample)
    Check-Dialogue ''
    $sample.synthetic_dialogue.mode = 'synthetic'
    $sample.synthetic_dialogue.backend = 'unknown'
    $view.SetStatus($sample)
    Check-Dialogue ''
    $sample.synthetic_dialogue.backend = 'postgresql'
    $view.SetStatus($sample)
    Check-Dialogue '已确认 1 · 回执不明 未提供 · 实际提交 3 · 拒绝 4 · 重复 0'

    foreach ($width in 820,690) {
        $window.Width = $width
        [Windows.Forms.Application]::DoEvents()
        if ($view.HorizontalScroll.Visible) { throw "窗口宽度 $width 时出现横向滚动。" }
        $checks.Add("窗口宽度 $width 无横向滚动")
    }
    Save-View 'view-synthetic.png'
    $sample.runtime_instance = '33333333333333333333333333333333'
    $sample.heartbeat_count = 1
    $view.SetStatus($sample)
    Check-Text SampleState '已采样 · 后台已重启'
    Check-Text RuntimeInstance $sample.runtime_instance
    Check-Text HeartbeatCount '1'
    Check-Text Completed '1'
    $sample.installation_id = '44444444444444444444444444444444'
    [void]$sample.Remove('synthetic_dialogue')
    [void]$sample.Remove('platforms')
    [void]$sample.Remove('progress')
    $view.SetStatus($sample)
    Check-Text SampleState '已采样 · 已切换服务器安装'
    Check-Dialogue ''
    Check-Text InstallationId $sample.installation_id
    Check-Text Completed '未提供'
    Check-Text LastCompletedId '未提供'
    Check-Text PlatformLocal '未提供'
    $view.SetConnection('error', '连接未获授权')
    Check-Text ConnectionState '连接错误'
    Check-Text ConnectionDetail '连接未获授权'
    Check-Text SampleState '离线 · 上次采样'
    $syntheticCount = $checks.Count
    if ($EndpointFile) {
        $window.Close()
        $options['profile'] = (Resolve-Path -LiteralPath $EndpointFile).Path
        $window = [Activator]::CreateInstance($windowType, [Reflection.BindingFlags]'Instance, Public, NonPublic',
            $null, [object[]](,$options), $null)
        $window.ShowInTaskbar = $false
        $window.Opacity = 0
        $window.Show()
        $view = $window.Controls.Find('StatusView', $true)[0]
        Check-Text SampleState '尚无采样'
        $connect = $window.Controls.Find('Connect', $true)[0]
        $disconnect = $window.Controls.Find('Disconnect', $true)[0]
        $sampleProperty = $windowType.GetProperty('LastSample', [Reflection.BindingFlags]'Instance, NonPublic')
        foreach ($phase in 'first', 'reconnected') {
            [Threading.SynchronizationContext]::SetSynchronizationContext([Windows.Forms.WindowsFormsSynchronizationContext]::new())
            $connect.PerformClick()
            $elapsed = [Diagnostics.Stopwatch]::StartNew()
            while ((Read-Value ConnectionState) -ne '已连接') {
                [Windows.Forms.Application]::DoEvents()
                if ((Read-Value ConnectionState) -eq '连接错误' -or $elapsed.Elapsed.TotalSeconds -gt 30) {
                    throw ("SSH 采样失败：" + (Read-Value ConnectionDetail))
                }
                Start-Sleep -Milliseconds 30
            }
            $actual = $sampleProperty.GetValue($window)
            $at = [DateTimeOffset]::Parse($actual.generated_at, [Globalization.CultureInfo]::InvariantCulture)
            Check-Text GeneratedAt ($at.UtcDateTime.ToString('yyyy-MM-dd HH:mm:ss.FFFFFFF') + ' UTC')
            Check-Text InstallationId $actual.installation_id
            Check-Text RuntimeInstance $actual.runtime_instance
            Check-Text HeartbeatCount ([string]$actual.heartbeat_count)
            foreach ($key in 'queued','running','completed','failed') {
                Check-Text $key ([string]$actual.progress[$key])
            }
            $last = $actual.progress.last_completed_id
            if ($null -eq $last -and $actual.progress.completed -eq 0) { $last = '暂无完成记录' }
            Check-Text LastCompletedId $last
            $dialogue = $actual['synthetic_dialogue']
            $line = ''
            if ($null -ne $dialogue -and $dialogue.backend -ceq 'postgresql' -and $dialogue.mode -ceq 'synthetic') {
                $parts = foreach ($key in 'confirmed','uncertain','submissions','denied','replays') {
                    if ($dialogue.ContainsKey($key)) { $dialogue[$key] } else { '未提供' }
                }
                $line = '已确认 {0} · 回执不明 {1} · 实际提交 {2} · 拒绝 {3} · 重复 {4}' -f $parts
            }
            Check-Dialogue $line
            foreach ($key in 'local','qq','wechat','bilibili') {
                $item = $actual.platforms[$key]
                $platformText = Read-Value ('Platform' + $key)
                if (-not $platformText.Contains($item.reason)) { throw "$key 平台原因与实际返回不符。" }
                $checks.Add("$key = $platformText")
            }
            $live[$phase] = [ordered]@{ generated_at=$actual.generated_at; runtime_instance=$actual.runtime_instance
                installation_id=$actual.installation_id; heartbeat_count=$actual.heartbeat_count
                synthetic_dialogue=$dialogue; dialogue_line=$line
                completed=$actual.progress.completed; last_completed_id=$actual.progress.last_completed_id }
            Save-View ("view-ssh-" + $phase + ".png")
            $disconnect.PerformClick()
            Check-Text ConnectionState '已断开'
            Check-Text SampleState '离线 · 上次采样'
            Check-Dialogue $line
            Check-Text RuntimeInstance $actual.runtime_instance
            if ($phase -eq 'first') { Save-View 'view-ssh-offline.png' }
        }
        if ($live.first.generated_at -eq $live.reconnected.generated_at) {
            throw '重连后没有取得新的服务端采样。'
        }
    }

    $passed = $true
} catch {
    $failure = $_.Exception.Message
    throw
} finally {
    $window.Close()
    $result = [ordered]@{ passed=$passed; synthetic_display_checks=$syntheticCount; ssh_samples=$live; host=$windowType.FullName
        executable_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash
        checks=$checks.ToArray(); error=$failure }
    $result | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $EvidenceDirectory 'display.json')
}
Write-Output ("{0} checks passed. {1}" -f $checks.Count, $EvidenceDirectory)
