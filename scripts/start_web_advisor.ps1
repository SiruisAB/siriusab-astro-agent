# Opens a dedicated, interactive login browser only when the user runs this script.
# This is not called by an agent, startup warmup, settings save, or tool import.
param([string]$PythonExecutable)

$ErrorActionPreference = 'Stop'
$webRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location -LiteralPath $webRoot
try {
    # Stdin avoids native argument quote stripping in Windows PowerShell 5.1.
    if (-not $PythonExecutable) {
        $PythonExecutable = @('.conda\python.exe', '.conda\Scripts\python.exe') |
            ForEach-Object { Join-Path $webRoot $_ } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    }
    if (-not $PythonExecutable) { throw 'Cannot locate the project Python environment.' }
    $webReadSettings = 'import json; from workbench_settings import WorkbenchSettings; print(json.dumps(WorkbenchSettings().load()["web_advisor"]))'
    $webSettingsJson = $webReadSettings | & $PythonExecutable -X utf8 -
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the local web-advisor settings.' }
    $webSettings = $webSettingsJson | ConvertFrom-Json
    $webEndpoint = [Uri]$webSettings.cdp_endpoint
    $webHost = $webEndpoint.DnsSafeHost.Trim('[', ']')
    if ($webSettings.browser_type -eq 'fingerprint') {
        Write-Output 'Fingerprint browser mode: this launcher does not start a normal Chrome/Edge or modify the fingerprint manager.'
        Write-Output "Start a dedicated Chromium profile in your fingerprint browser, enable local CDP, and set its endpoint in this project: $($webSettings.cdp_endpoint)"
        Write-Output "Apply the selected proxy in that browser profile and restart it. Mode: $($webSettings.proxy_mode)"
        if ($webSettings.proxy_mode -eq 'custom') { Write-Output "Local proxy: $($webSettings.proxy_url)" }
        Write-Output 'Then confirm the applied proxy in workbench settings. CDP itself always connects directly over loopback. No browser was launched or stopped.'
        return
    }
    $webProbe = [Net.Sockets.TcpClient]::new()
    try {
        $webConnect = $webProbe.ConnectAsync($webHost, $webEndpoint.Port)
        try { $webOccupied = $webConnect.Wait(600) -and $webProbe.Connected } catch { $webOccupied = $false }
        if ($webOccupied) { throw 'CDP port is already occupied. Verify that browser manually or choose another CDP port in workbench settings. No process was stopped.' }
    } finally { $webProbe.Dispose() }
    $webProxyArgs = @()
    if ($webSettings.proxy_mode -eq 'direct') {
        $webProxyArgs += '--no-proxy-server'
    } elseif ($webSettings.proxy_mode -eq 'custom') {
        $webProxyUri = [Uri]$webSettings.proxy_url
        $webProxyProbe = [Net.Sockets.TcpClient]::new()
        try {
            $webProxyConnect = $webProxyProbe.ConnectAsync($webProxyUri.DnsSafeHost.Trim('[', ']'), $webProxyUri.Port)
            try { $webProxyReady = $webProxyConnect.Wait(1000) -and $webProxyProbe.Connected } catch { $webProxyReady = $false }
            if (-not $webProxyReady) { throw 'Selected local proxy is not listening. No browser was launched, and no direct fallback was used.' }
        } finally { $webProxyProbe.Dispose() }
        $webProxyArgs += "--proxy-server=$($webSettings.proxy_url)"
        $webProxyArgs += '--disable-quic'
    }
    # Browser precedence: explicit override, then lightweight Chromium builds,
    # then Chrome, then Edge (largest footprint and bundles more background services).
    # Set ASTRO_WEB_BROWSER to an absolute exe path to force a specific browser.
    $webCandidates = @()
    if ($env:ASTRO_WEB_BROWSER) {
        $webCandidates += $env:ASTRO_WEB_BROWSER
    }
    $webCandidates += @(
        'C:\Program Files\Thorium\Application\thorium.exe',
        (Join-Path $env:LOCALAPPDATA 'Thorium\Application\thorium.exe'),
        (Join-Path $env:LOCALAPPDATA 'Chromium\Application\chrome.exe'),
        'C:\Program Files\Chromium\Application\chrome.exe',
        (Join-Path $env:LOCALAPPDATA 'ungoogled-chromium\Application\chrome.exe'),
        'C:\Program Files\Google\Chrome\Application\chrome.exe',
        'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
        'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe')
    )
    $webBrowser = $webCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $webBrowser) { throw 'Chrome or Edge was not found. No browser was installed.' }
    $webProfile = [IO.Path]::GetFullPath((Join-Path $webRoot '.workbench\web-advisor-profile'))
    if (-not $webProfile.StartsWith($webRoot.TrimEnd('\') + '\.workbench\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid dedicated profile path.' }
    foreach ($webDirectory in @($webRoot, (Join-Path $webRoot '.workbench'), $webProfile)) {
        if (Test-Path -LiteralPath $webDirectory) {
            if ((Get-Item -LiteralPath $webDirectory -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked profile directories are not allowed.' }
        }
    }
    New-Item -ItemType Directory -Path $webProfile -Force | Out-Null
    $webUrls = @()
    if ($webSettings.providers -contains 'chatgpt') { $webUrls += 'https://chatgpt.com/' }
    if ($webSettings.providers -contains 'claude') { $webUrls += 'https://claude.ai/new' }
    if ($webSettings.providers -contains 'gemini') { $webUrls += 'https://gemini.google.com/app' }
    if (-not $webUrls.Count) { $webUrls = @('about:blank') }
    $webBrowserArgs = @('--new-window', '--no-first-run', '--no-default-browser-check', '--window-size=1100,800',
        # The advisor profile only needs cookies for login. Extensions and component
        # updaters are pure overhead here (measured: +7 processes, ~1.4 GB total).
        '--disable-extensions', '--disable-component-update', '--disable-background-networking',
        "--remote-debugging-address=$webHost", "--remote-debugging-port=$($webEndpoint.Port)", "--user-data-dir=`"$webProfile`"") + $webProxyArgs + $webUrls
    # Visible because this user-invoked browser requires interactive login.
    $webProcess = Start-Process -FilePath $webBrowser -ArgumentList $webBrowserArgs -WindowStyle Normal -PassThru
    # Record only the instance this launcher started, so idle auto-close can never
    # touch a browser the user opened manually or a fingerprint browser.
    $webMarker = Join-Path $webRoot '.workbench\web-advisor-browser.json'
    @{ pid = $webProcess.Id; exe = $webBrowser; started = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() } |
        ConvertTo-Json | Set-Content -LiteralPath $webMarker -Encoding UTF8
    Write-Output "Dedicated login browser opened. CDP: $($webSettings.cdp_endpoint)"
    Write-Output "Browser proxy mode: $($webSettings.proxy_mode). Direct mode bypasses browser proxies, not OS-level TUN/VPN routing."
    Write-Output 'Log in manually and select the web model. Then enable Web Advisor in this project workbench settings. Do not share the browser profile or expose the CDP port.'
} finally { Pop-Location }
