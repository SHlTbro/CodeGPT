# Local Codebase MCP one-command deploy (zero-dependency friendly)
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File deploy.ps1 [-Port 8168]
# NOTE: 本文件含中文注释，必须保存为 UTF-8 with BOM（Windows PowerShell 5.1 / ACP=936 下无 BOM 中文乱码）
# Output: local MCP server + cloudflared public tunnel + a URL to paste into ChatGPT
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$Port = 8168
$TunnelToken = $null
$DryRunTunnel = $false
for ($i = 0; $i -lt $args.Count; $i++) {
  if ($args[$i] -eq '-Port') { $Port = [int]$args[$i + 1] }
  elseif ($args[$i] -eq '-TunnelToken') { $TunnelToken = ([string]$args[$i + 1]) -replace '\s+', '' }
  elseif ($args[$i] -eq '-DryRunTunnel') { $DryRunTunnel = $true }
}
# named tunnel token 优先级：-TunnelToken 参数 > 脚本目录 tunnel-token.txt（Cloudflare dashboard 复制的一串密文，多余空行/换行自动 trim）> 无（走 trycloudflare 快速隧道）
if (-not $TunnelToken -and (Test-Path "$root\tunnel-token.txt")) {
  $TunnelToken = ((Get-Content "$root\tunnel-token.txt" -Raw) -replace '\s+', '')
}
if ($TunnelToken -eq '') { $TunnelToken = $null }

# DryRun 提前出口（C-020）：只打印隧道参数，绝不执行步骤 1-6——防止测试/干跑重写 config.runtime.yaml 干扰正在运行的热重载 server
if ($DryRunTunnel) {
  $TunnelMode = 'quick'
  if ($TunnelToken) { $TunnelMode = 'named' }
  if ((Test-Path "$root\ngrok-token.txt") -and (Test-Path "$root\ngrok-domain.txt")) { $TunnelMode = 'ngrok' }
  $dryPort = $Port
  $dryArgs = switch ($TunnelMode) {
    'ngrok'  { @('http', "--url=$(((Get-Content "$root\ngrok-domain.txt" -Raw) -replace '\s+',''))", '--log', 'stdout', "$dryPort") }
    'named'  { @('tunnel', 'run', '--protocol', 'http2') }
    default  { @('tunnel', '--url', "http://127.0.0.1:$dryPort", '--protocol', 'http2', '--loglevel', 'info') }
  }
  Write-Host "[deploy] DRYRUN tunnel-mode=$TunnelMode"
  Write-Host "[deploy] DRYRUN tunnel-argv=$($dryArgs -join ' ')"
  Write-Host '[deploy] dry run complete: nothing started, no files touched'
  exit 0
}

function Info($m) { Write-Host "[deploy] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Ok($m)   { Write-Host "[done] $m" -ForegroundColor Green }

# 名单冲突：已有 cloudflared 在跑则提示后退出，不自动杀（防误伤其他隧道的进程）
if (-not $DryRunTunnel -and ((Get-Process cloudflared -ErrorAction SilentlyContinue) -or (Get-Process ngrok -ErrorAction SilentlyContinue))) {
  Warn "已有 cloudflared 隧道进程在运行：先结束旧隧道进程再重跑（任务管理器结束 cloudflared.exe，或 Stop-Process -Name cloudflared）。脚本不自动结束，防误伤。"
  exit 1
}

# ---------- 1. Python ----------
Info "1/8 check Python (>=3.12) ..."
$py = $null
foreach ($c in @('python', 'py')) {
  $cmd = Get-Command $c -ErrorAction SilentlyContinue
  if ($cmd) {
    $v = & $cmd.Source -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($v -match '^3\.(\d+)') { $minor = [int]$Matches[1]; if ($minor -ge 12) { $py = $cmd.Source; break } }
    if ($c -eq 'py') { $py = $cmd.Source } # py launcher picks py3 by default
  }
}
if (-not $py) {
  Info "Python not found, trying winget install ..."
  try { winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent | Out-Null } catch {}
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { $py = $cmd.Source } else { Warn "auto-install failed: install Python 3.12+ from https://www.python.org/downloads/ then rerun"; exit 1 }
}
Ok "Python: $py"

# ---------- 2. venv + deps ----------
Info "2/8 prepare venv and dependencies ..."
if (-not (Test-Path "$root\.venv")) { & $py -m venv "$root\.venv" }
$vpy = "$root\.venv\Scripts\python.exe"
$eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue' # native stderr must not become terminating under EAP=Stop
& "$root\.venv\Scripts\pip.exe" install -q -r "$root\requirements.txt" 2>&1 | Out-Null
$pipCode = $LASTEXITCODE
if ($pipCode -ne 0) {
  Warn "PyPI install failed, retry with Tsinghua mirror ..."
  & "$root\.venv\Scripts\pip.exe" install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r "$root\requirements.txt" 2>&1 | Out-Null
  $pipCode = $LASTEXITCODE
}
$ErrorActionPreference = $eap
if ($pipCode -ne 0) { Warn "pip install failed: check network then rerun"; exit 1 }
Ok "dependencies ready (fastmcp/pyyaml)"

# ---------- 3. rg / git ----------
Info "3/8 check ripgrep / git ..."
if (-not (Get-Command rg -ErrorAction SilentlyContinue) -and -not (Test-Path "$root\bin\rg.exe")) {
  try { winget install BurntSushi.ripgrep.MSVC --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null } catch {}
}
if (-not (Get-Command rg -ErrorAction SilentlyContinue) -and -not (Test-Path "$root\bin\rg.exe")) {
  # 自动下载 rg 到 bin\（走镜像，GitHub 直连失败自动切换）
  $got = $false
  foreach ($u in @(
    'https://gh-proxy.com/https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-pc-windows-msvc.zip',
    'https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-pc-windows-msvc.zip'
  )) {
    try {
      Invoke-WebRequest -Uri $u -OutFile "$root\rg.zip" -UseBasicParsing -TimeoutSec 120
      Expand-Archive -Path "$root\rg.zip" -DestinationPath "$root\rg_tmp" -Force
      Copy-Item "$root\rg_tmp\*\rg.exe" "$root\bin\rg.exe" -Force
      Remove-Item "$root\rg.zip", "$root\rg_tmp" -Recurse -Force -ErrorAction SilentlyContinue
      $got = $true; break
    } catch { }
  }
  if (-not $got) { Warn "ripgrep download failed. Fix: winget install BurntSushi.ripgrep.MSVC" }
}
# bin\rg.exe 存在即视为可用：启动 server 时把 bin\ 前置进 PATH（子进程继承）
if (-not (Get-Command rg -ErrorAction SilentlyContinue) -and (Test-Path "$root\bin\rg.exe")) { Ok "ripgrep OK (bundled bin\rg.exe)" }
elseif (Get-Command rg -ErrorAction SilentlyContinue) { Ok "ripgrep OK" }
else { Warn "ripgrep missing: search_code/list_files will error. Fix: winget install BurntSushi.ripgrep.MSVC" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Warn "git missing: git_info will error. Fix: https://git-scm.com" } else { Ok "git OK" }

# ---------- 4. cloudflared ----------
Info "4/8 check cloudflared ..."
if (-not (Test-Path "$root\cloudflared.exe")) {
  $urls = @(
    'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe',
    'https://gh-proxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
  )
  $got = $false
  foreach ($u in $urls) {
    try { Invoke-WebRequest -Uri $u -OutFile "$root\cloudflared.exe" -UseBasicParsing -TimeoutSec 180; $got = $true; break } catch { }
  }
  if (-not $got) { Warn "cloudflared download failed on both channels. Manually download cloudflared-windows-amd64.exe into this folder and rerun"; exit 1 }
}
Ok "cloudflared OK"

# ---------- 5. config.yaml ----------
Info "5/8 check config.yaml ..."
if (-not (Test-Path "$root\config.yaml")) {
  $tpl = @"
# Local Codebase MCP project registry: AI can ONLY see projects listed here.
projects:
  - id: demo
    name: Demo
    path: "$root"
    # write_enabled: true  ← 允许 AI 写该项目 docs/**（热重载生效无需重启；默认关闭，见 README「写能力」节）

# Add your real projects in the same format, then rerun deploy.bat:
#  - id: myproj
#    name: My Project
#    path: D:/path/to/myproj

server:
  host: 127.0.0.1
  port: $Port
  path: /mcp
"@
  $tpl | Out-File -Encoding utf8 "$root\config.yaml"
  Warn "config.yaml template generated (demo project registered). Edit it and rerun deploy.bat"
} else { Ok "config.yaml exists (keeping your config)" }

# ---------- 6. free port + runtime config ----------
Info "6/8 pick free port and build runtime config ..."
function Test-PortFree($p) {
  $t = New-Object Net.Sockets.TcpClient
  try { $t.Connect('127.0.0.1', $p); $t.Close(); return $false } catch { return $true } finally { $t.Dispose() }
}
# named 模式下端口必须与 Cloudflare dashboard 的 Service 配置一致，禁止自动漂移（漂移=dashboard 指向失效）
while (-not (Test-PortFree $Port)) {
  if ($TunnelToken) { Warn "port $Port is busy. In named-tunnel mode the port MUST match the dashboard Service mapping (no auto-drift). Free the port or update the dashboard, then rerun"; exit 1 }
  Info "port $Port busy, trying $($Port + 1)"; $Port++
}
$pyPatch = "import yaml,sys; p=sys.argv[1]; d=yaml.safe_load(open(p,encoding='utf-8')); d.setdefault('server',{}); d['server'].update({'host':'127.0.0.1','port':$Port,'path':'/mcp'}); yaml.safe_dump(d,open(sys.argv[2],'w',encoding='utf-8'),allow_unicode=True)"
& $vpy -c $pyPatch "$root\config.yaml" "$root\config.runtime.yaml"
if ($LASTEXITCODE -ne 0) { Warn "config.yaml parse failed (syntax/fields). Fix the python error above then rerun"; exit 1 }
Ok "runtime config: config.runtime.yaml (port=$Port)"

# ---------- 7. start ----------
Info "7/8 start MCP server and tunnel ..."
# 隧道模式优先级：ngrok（固定域，需 ngrok-token.txt + ngrok-domain.txt + ngrok.exe）> cloudflared named（tunnel-token.txt）> quick（trycloudflare 零账号）
# 各模式均强制 --protocol http2 / 等价 TCP（C-016）；注意（C-018）：`tunnel run` 子命令不认任何 log 旗标，named 裸 run
$ngrokToken = $null; $ngrokDomain = $null
if (Test-Path "$root\ngrok-token.txt") { $ngrokToken = ((Get-Content "$root\ngrok-token.txt" -Raw) -replace '\s+', '') }
if (Test-Path "$root\ngrok-domain.txt") { $ngrokDomain = ((Get-Content "$root\ngrok-domain.txt" -Raw) -replace '\s+', '') }
if ($ngrokToken -and $ngrokDomain) {
  $TunnelMode = 'ngrok'
} elseif ($TunnelToken) {
  $TunnelMode = 'named'
} else {
  $TunnelMode = 'quick'
}
if ($TunnelMode -eq 'ngrok') {
  $env:NGROK_AUTHTOKEN = $ngrokToken
  $tunnelArgs = @('http', "--url=$ngrokDomain", '--log', 'stdout', "$Port")
} elseif ($TunnelMode -eq 'named') {
  $tunnelArgs = @('tunnel', 'run', '--protocol', 'http2')
} else {
  $tunnelArgs = @('tunnel', '--url', "http://127.0.0.1:$Port", '--protocol', 'http2', '--loglevel', 'info')
}
$serverWin = "title Local-Codebase-MCP Server (port $Port) && `"$vpy`" `"$root\server.py`" --config `"$root\config.runtime.yaml`""
# bin\rg.exe 存在时前置进 PATH（server 子进程继承，ripgrep 随包分发不依赖系统安装）
if (Test-Path "$root\bin\rg.exe") { $env:PATH = "$root\bin;$env:PATH" }
# server 以隐藏常驻方式启动（C-022：cmd 窗口被关/机器休眠=server 死，已两次断连）；PID 记入 server.pid 供 stop-all.bat 精确停止
$svp = Start-Process -FilePath "$vpy" -ArgumentList 'server.py', '--config', "$root\config.runtime.yaml" -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput "$root\server-access.log" -RedirectStandardError "$root\server-access.err"
Set-Content -Encoding ascii "$root\server.pid" $svp.Id
Remove-Item "$root\tunnel.log", "$root\tunnel.err", "$root\tunnel-ngrok.log" -ErrorAction SilentlyContinue
if ($TunnelMode -eq 'ngrok') {
  # token 经 NGROK_AUTHTOKEN 环境变量传给子进程，不落在命令行/日志/文件
  $env:NGROK_AUTHTOKEN = $ngrokToken
  $ngrokExe = "$root\ngrok.exe"
  if (-not (Test-Path $ngrokExe)) { $cmdNgrok = Get-Command ngrok -ErrorAction SilentlyContinue; if ($cmdNgrok) { $ngrokExe = $cmdNgrok.Source } }
  if (-not (Test-Path $ngrokExe) -and -not $cmdNgrok) { Warn "ngrok not found: install via 'winget install ngrok.ngrok' or download ngrok.exe into this folder, then rerun"; exit 1 }
  Start-Process -FilePath $ngrokExe -ArgumentList $tunnelArgs -RedirectStandardOutput "$root\tunnel-ngrok.log" -RedirectStandardError "$root\tunnel-ngrok.err" -WindowStyle Hidden | Out-Null
  Remove-Item Env:NGROK_AUTHTOKEN -ErrorAction SilentlyContinue
  Ok "ngrok tunnel started (token via NGROK_AUTHTOKEN env)"
} elseif ($TunnelMode -eq 'named') {
  # token 经 TUNNEL_TOKEN 环境变量传给子进程（cloudflared 原生支持），不落在命令行/日志/文件
  $env:TUNNEL_TOKEN = $TunnelToken
  Start-Process -FilePath "$root\cloudflared.exe" -ArgumentList $tunnelArgs -RedirectStandardOutput "$root\tunnel.log" -RedirectStandardError "$root\tunnel.err" -WindowStyle Hidden | Out-Null
  Remove-Item Env:TUNNEL_TOKEN -ErrorAction SilentlyContinue
  Ok "named tunnel started (token via TUNNEL_TOKEN env)"
} else {
  # --protocol http2: 强制 TCP；QUIC(UDP) 在部分运营商网络下反复掉线（C-016 实测根因）
  Start-Process -FilePath "$root\cloudflared.exe" -ArgumentList 'tunnel', '--url', "http://127.0.0.1:$Port", '--protocol', 'http2', '--loglevel', 'info' -RedirectStandardOutput "$root\tunnel.log" -RedirectStandardError "$root\tunnel.err" -WindowStyle Hidden | Out-Null
}

# ---------- 8. public URL + health ----------
$publicUrl = $null
if ($TunnelMode -eq 'ngrok') {
  Info "8/8 ngrok mode: URL is the fixed domain in ngrok-domain.txt"
  $publicUrl = "https://$ngrokDomain/mcp"
} elseif ($TunnelToken) {
  Info "8/8 named tunnel mode: skip trycloudflare URL capture (public hostname is fixed in Cloudflare dashboard)"
} else {
  Info "8/8 waiting for tunnel URL (up to 60s) ..."
  for ($s = 0; $s -lt 60; $s++) {
    Start-Sleep -Seconds 1
    $hit = Select-String -Path "$root\tunnel.log", "$root\tunnel.err" -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { $publicUrl = $hit.Matches[0].Value; break }
  }
}
$alive = $false
# 健康检查重试 25s（C-023）：多项目 venv 冷启动可能超过单次 8s，单发误报会导致 deploy 报红但实际已起
for ($h = 0; $h -lt 25 -and -not $alive; $h++) {
  Start-Sleep -Seconds 1
  try { Invoke-WebRequest "http://127.0.0.1:$Port/mcp" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop | Out-Null; $alive = $true } catch {
    if ($_.Exception.Response) { $alive = $true } # any HTTP response (e.g. 406/400) = server is listening
  }
}
Write-Host ''
if (-not $alive) { Warn "local health check failed: read the Server window error (usually config.yaml issue)"; exit 1 }
Ok "local server alive: http://127.0.0.1:$Port/mcp"
if ($TunnelMode -eq 'ngrok') {
  Ok "ngrok tunnel started, permanent URL: $publicUrl"
  Set-Content -Encoding ascii "$root\public-url.txt" $publicUrl
  Write-Host ''
  Write-Host '==================== FINAL STEP ====================' -ForegroundColor Green
  Write-Host 'Paste this into ChatGPT connector "Server URL" (auth: none):' -ForegroundColor Green
  Write-Host $publicUrl -ForegroundColor White
  Write-Host '====================================================' -ForegroundColor Green
  Write-Host ''
  Write-Host 'Notes:'
  Write-Host ' 1) ngrok domain is permanent: server restarts / config changes never change it.'
  Write-Host ' 2) Stop: close the Server window + kill ngrok.exe in Task Manager.'
  Write-Host ' 3) Free ngrok shows a warning page for BROWSER visits only; MCP API calls are unaffected.'
} elseif ($TunnelToken) {
  Ok "named tunnel 已启动，公网地址=你在 Cloudflare dashboard 配置的 Public Hostname（形如 https://mcp.你的域名/mcp）"
  $existing = $null
  if (Test-Path "$root\public-url.txt") { $existing = Get-Content "$root\public-url.txt" -Raw -ErrorAction SilentlyContinue }
  if ($existing -and $existing.Trim()) {
    Info "public-url.txt 已有内容，named 模式不覆盖（URL 由 owner 维护）"
  } else {
    Set-Content -Encoding utf8 "$root\public-url.txt" "NAMED_TUNNEL: 在此填入你 Cloudflare dashboard 的 Public Hostname（形如 https://mcp.你的域名/mcp）"
    Info "public-url.txt 写入占位说明，请替换为真实地址"
  }
  Write-Host ''
  Write-Host '==================== FINAL STEP ====================' -ForegroundColor Green
  Write-Host 'ChatGPT connector "Server URL" (permanent, same across restarts):' -ForegroundColor Green
  Write-Host 'https://mcp.你的域名/mcp   (= your Cloudflare dashboard Public Hostname + /mcp)' -ForegroundColor White
  Write-Host '====================================================' -ForegroundColor Green
  Write-Host ''
  Write-Host 'Notes:'
  Write-Host ' 1) named tunnel 地址永久固定：重启 deploy.bat 后无需更新 ChatGPT 连接器。'
  Write-Host ' 2) dashboard 里 Public Hostname 的 Service 端口必须与 runtime 实际端口一致（config.yaml 固定 server.port 最稳）。'
  Write-Host ' 3) Stop: close the Server window + kill cloudflared.exe in Task Manager.'
} elseif ($publicUrl) {
  $mcpUrl = "$publicUrl/mcp"
  Ok "public URL: $mcpUrl"
  Set-Content -Encoding ascii "$root\public-url.txt" $mcpUrl
  Write-Host ''
  Write-Host '==================== FINAL STEP ====================' -ForegroundColor Green
  Write-Host 'Paste this into ChatGPT connector "Server URL" (auth: none):' -ForegroundColor Green
  Write-Host $mcpUrl -ForegroundColor White
  Write-Host '====================================================' -ForegroundColor Green
  Write-Host ''
  Write-Host 'Notes:'
  Write-Host ' 1) Opening the public URL in your own browser may fail (DNS blocked in CN) - this does NOT affect ChatGPT.'
  Write-Host ' 2) Tunnel URL changes on every restart: rerun this script and paste the new URL.'
  Write-Host ' 3) Stop: close the Server window + kill cloudflared.exe in Task Manager.'
} else {
  Warn "no tunnel URL within 60s. Check tunnel.log / tunnel.err; common cause: restricted outbound network (see docs/deploy-experience.md item 4)"
}
