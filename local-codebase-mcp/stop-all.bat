@echo off
rem Stop CodeGPT through the local authenticated control plane, never by process name.
chcp 65001 >nul
powershell -NoProfile -Command "$u='http://127.0.0.1:8169';try{$i=Invoke-RestMethod ($u+'/api/info');$expected=[IO.Path]::GetFullPath('%~dp0config.toml');if($i.name-ne'local-codebase-mcp-setup'-or$i.config-ne$expected){throw 'Different Setup owner'};$s=Invoke-RestMethod ($u+'/api/session');Invoke-RestMethod -Method Post -Uri ($u+'/api/stop') -Headers @{'X-Setup-Token'=$s.token} -ContentType 'application/json' -Body '{}'|Out-Null;Write-Host 'CodeGPT: Stopped this Setup instance.'}catch{Write-Host 'CodeGPT Setup is not reachable on port 8169. Close its owner window or use its configured local port.'}"
pause
