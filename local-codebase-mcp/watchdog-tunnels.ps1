# Compatibility entry point. Runtime supervises only processes it owns.
& (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'setup.ps1') @args
exit $LASTEXITCODE
