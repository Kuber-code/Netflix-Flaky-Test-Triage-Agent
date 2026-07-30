# Windows shim for the Makefile targets. GNU make is not present on a stock
# Windows box, and CI runs the Makefile on Linux; this keeps the two in step so
# a contributor on Windows can run the same gate. Usage: .\make.ps1 check
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'install', 'check', 'lint', 'fmt', 'typecheck', 'test', 'cov', 'eval', 'eval-baseline', 'clean')]
    [string]$Target = 'help'
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Invoke-Step {
    param([string[]]$CommandArgs)
    Write-Host "> uv $($CommandArgs -join ' ')" -ForegroundColor DarkGray
    & uv @CommandArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: uv $($CommandArgs -join ' ')" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

$steps = @{
    install   = @( , @('sync', '--all-groups'))
    lint      = @(@('run', 'ruff', 'check', '.'), @('run', 'ruff', 'format', '--check', '.'))
    fmt       = @(@('run', 'ruff', 'format', '.'), @('run', 'ruff', 'check', '--fix', '.'))
    typecheck = @( , @('run', 'mypy'))
    test      = @( , @('run', 'pytest'))
    cov       = @(
        @('run', 'pytest', '--cov', '--cov-report=term-missing', '--cov-report=xml'),
        @('run', 'python', 'scripts/check_core_coverage.py'))
    eval      = @( , @('run', 'python', 'eval/run_eval.py'))
    'eval-baseline' = @( , @('run', 'python', 'eval/run_eval.py', '--no-llm'))
}
$steps['check'] = $steps['lint'] + $steps['typecheck'] + $steps['test']

switch ($Target) {
    'help' {
        Write-Host 'Targets: install check lint fmt typecheck test cov eval eval-baseline clean'
    }
    'clean' {
        $paths = @('.pytest_cache', '.mypy_cache', '.ruff_cache', '.hypothesis',
            '.coverage', 'coverage.xml', 'htmlcov', '.flaketriage')
        foreach ($p in $paths) {
            if (Test-Path $p) { Remove-Item -Recurse -Force $p }
        }
        Write-Host 'Cleaned.'
    }
    default {
        foreach ($step in $steps[$Target]) { Invoke-Step -CommandArgs $step }
        Write-Host "$Target OK" -ForegroundColor Green
    }
}
