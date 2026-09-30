param(
    [Parameter(Mandatory = $true)][string]$ScriptPath,
    [string]$OutputDirectory = $PSScriptRoot,
    [string]$ActionName = 'Anki 图片编辑并回链',
    [string]$ActionId = '582dbef2-4b09-4b5b-a72e-87d88307c321',
    [string]$References = '',
    [string]$Description = '从 Anki 浏览窗口提取图片，Everything 定位并用画图编辑，保存后写回 Anki Note Linker 原图笔记链接。'
)

$ErrorActionPreference = 'Stop'
$scriptAbsolutePath = (Resolve-Path -LiteralPath $ScriptPath).Path
$outputAbsolutePath = (Resolve-Path -LiteralPath $OutputDirectory).Path
$scriptText = [System.IO.File]::ReadAllText($scriptAbsolutePath)
if ($scriptText -notmatch 'public\s+static\s+\w+\s+Exec\s*\(') {
    throw 'C# 源文件必须包含 public static ... Exec(...) 入口。'
}

# This is Quicker 1.x's exported ActionItem schema, verified against the
# installed Quicker 1.45.5 public data types and its pure ActionConverter.
# It does not access or change Quicker's database or user settings.
$inputParams = [ordered]@{}
foreach ($entry in ([ordered]@{
    mode = 'normal_roslyn'
    script = $scriptText
    reference = $References
    runOnUiThread = 'staLongRun'
    stopIfFail = '1'
}).GetEnumerator()) {
    $inputParams[$entry.Key] = [ordered]@{ VarKey = $null; Value = $entry.Value }
}
$definition = [ordered]@{
    LimitSingleInstance = $true
    SummaryExpression = '$$'
    SubPrograms = @()
    Variables = @()
    Steps = @([ordered]@{
        StepRunnerKey = 'sys:csscript'
        InputParams = $inputParams
        OutputParams = @{}
        IfSteps = $null
        ElseSteps = $null
        Note = '提取图片 → Everything 定位 → 画图编辑 → 保存后自动回链'
        Disabled = $false
        Collapsed = $false
        DelayMs = 0
    })
}
$definitionJson = ConvertTo-Json -InputObject $definition -Depth 30
$utcTimestamp = [DateTime]::UtcNow.ToString('o')
$action = [ordered]@{
    Row = 0
    Col = 0
    ActionType = 24
    Title = $ActionName
    Description = $Description
    Icon = 'fa:Light_Image:#6aaded'
    Path = $null
    DelayMs = 0
    Data = $definitionJson
    Data2 = ''
    Data3 = ''
    Children = $null
    Id = $ActionId
    TemplateId = $null
    TemplateRevision = 0
    UseTemplate = $false
    LastEditTimeUtc = $utcTimestamp
    SharedActionId = $null
    ShareTimeUtc = $null
    CreateTimeUtc = $utcTimestamp
    AsSubProgram = $false
    SkipWhenStopRunningActions = $false
    SkipCheckUpdate = $false
    AutoUpdate = $false
    KeepInfoWhenUpdate = $false
    MinQuickerVersion = '1.40.16'
    ContextMenuData = $null
    AllowScrollTrigger = $false
    EnableEvaluateVariable = $true
    IsTextProcessor = $false
    IsImageProcessor = $false
    Association = $null
    DoNotClosePanel = $null
    UserLimitation = $null
}
$actionJson = ConvertTo-Json -InputObject $action -Depth 30

# Deserializing newly built objects is read-only; it verifies actual app
# contract compatibility without importing or executing this action.
$quickerInstallPath = 'C:\Program Files\Quicker'
if (Test-Path -LiteralPath (Join-Path $quickerInstallPath 'Quicker.exe')) {
    [void][Reflection.Assembly]::LoadFrom((Join-Path $quickerInstallPath 'Quicker.Common.dll'))
    [void][Reflection.Assembly]::LoadFrom((Join-Path $quickerInstallPath 'Newtonsoft.Json.dll'))
    [void][Reflection.Assembly]::LoadFrom((Join-Path $quickerInstallPath 'Quicker.exe'))
    $readAction = [Newtonsoft.Json.JsonConvert]::DeserializeObject($actionJson, [Quicker.Common.ActionItem])
    $readDefinition = [Newtonsoft.Json.JsonConvert]::DeserializeObject($readAction.Data, [Quicker.Domain.Actions.X.XAction])
    if ($readAction.ActionType -ne 24 -or $readDefinition.Steps.Count -ne 1 -or
        $readDefinition.Steps[0].StepRunnerKey -ne 'sys:csscript' -or
        $readDefinition.Steps[0].InputParams['script'].Value -cne $scriptText) {
        throw 'Quicker 动作格式回读校验失败。'
    }
}

# Generated outputs, mechanically serialized from the source above.
$jsonPath = Join-Path $outputAbsolutePath 'Anki图片编辑回链_导入Quicker.json'
$qkaPath = Join-Path $outputAbsolutePath 'Anki图片编辑回链_动作定义.qka'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($jsonPath, $actionJson, $utf8NoBom)
[System.IO.File]::WriteAllText($qkaPath, $definitionJson, $utf8NoBom)
[pscustomobject]@{
    ActionId = $ActionId
    FullActionImport = $jsonPath
    DefinitionOnly = $qkaPath
    VerifiedScriptCharacters = $scriptText.Length
}
