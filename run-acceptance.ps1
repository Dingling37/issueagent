# 从任意目录运行本脚本。它使用当前终端的密钥；没有密钥时只在本地隐藏输入。
# 不保存密钥、不自动提交代码。将真实任务交给 main.py，而不是模拟模型。
$ErrorActionPreference = 'Stop'
$agentPython = 'C:\Users\崔佑齐\PycharmProjects\learn-claude-code\.venv\Scripts\python.exe'
$agentRepo = Join-Path (Split-Path -Parent $PSScriptRoot) 'registration-test-repo'
$mavenBin = 'C:\Users\崔佑齐\.m2\wrapper\dists\apache-maven-3.9.10-bin\53h08a94dg6djh6umvruv7q564\apache-maven-3.9.10\bin'
$jdkPath = 'C:\Users\崔佑齐\.jdks\ms-17.0.17'
foreach ($requiredPath in @($agentPython, (Join-Path $mavenBin 'mvn.cmd'), (Join-Path $jdkPath 'bin\javac.exe'), (Join-Path $agentRepo 'ISSUE-002.md'))) {
    if (-not (Test-Path -LiteralPath $requiredPath)) { throw "Required file not found: $requiredPath" }
}
$previousPath = $env:PATH
$previousJava = $env:JAVA_HOME
$previousKey = $env:ANTHROPIC_API_KEY
$previousBase = $env:ANTHROPIC_BASE_URL
$previousModel = $env:MODEL_ID
$agentExitCode = 1
try {
    $env:JAVA_HOME = $jdkPath
    $env:PATH = "$mavenBin;$jdkPath\bin;$env:PATH"
    if (-not $env:ANTHROPIC_API_KEY) {
        $secret = Read-Host 'Enter your DeepSeek API key (hidden)' -AsSecureString
        $env:ANTHROPIC_API_KEY = ([System.Net.NetworkCredential]::new('', $secret)).Password
        $secret.Dispose()
    }
    if (-not $env:ANTHROPIC_API_KEY) { throw 'API key is empty' }
    # 本脚本专用于本轮 DeepSeek 验收，结束后恢复原有环境。
    $env:ANTHROPIC_BASE_URL = 'https://api.deepseek.com/anthropic'
    $env:MODEL_ID = 'deepseek-flash'
    $issueFile = Join-Path $agentRepo 'ISSUE-002.md'
    Write-Host 'Starting real-model acceptance. This will modify registration-test-repo and use API tokens.'
    & $agentPython (Join-Path $PSScriptRoot 'main.py') --repo $agentRepo --issue-file $issueFile --max-steps 20 --test-timeout 300
    $agentExitCode = $LASTEXITCODE
    Write-Host "Agent exit code: $agentExitCode. Check the result or error printed above; startup errors may have no outputs directory."
} finally {
    $env:PATH = $previousPath
    $env:JAVA_HOME = $previousJava
    $env:ANTHROPIC_API_KEY = $previousKey
    $env:ANTHROPIC_BASE_URL = $previousBase
    $env:MODEL_ID = $previousModel
}
exit $agentExitCode
