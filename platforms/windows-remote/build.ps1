[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (!(Test-Path -LiteralPath $compiler)) { throw '需要 Windows x64 .NET Framework 4.x C# 编译器。' }
$output = Join-Path $PSScriptRoot 'build'
[void][IO.Directory]::CreateDirectory($output)
$exe = Join-Path $output 'Qiban.Remote.exe'
$sources = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.cs' -File)
$sources += @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'Display') -Filter '*.cs' -File)
$result = Join-Path $output ('Qiban.Remote.' + [Guid]::NewGuid().ToString('N') + '.exe')
try {
    & $compiler /nologo /target:winexe /platform:x64 /optimize+ /codepage:65001 /langversion:5 "/out:$result" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll $sources.FullName
    if ($LASTEXITCODE) { throw '客户端编译失败。' }
    [IO.File]::Copy($result, $exe, $true)
} finally {
    if (Test-Path -LiteralPath $result) { Remove-Item -LiteralPath $result }
}
Write-Output $exe
