# Run from Windows PowerShell 5.1 or PowerShell 7 on the classroom server.
# This prepares the new stack on 127.0.0.1:5482. It does not edit Caddy.
[CmdletBinding()]
param(
    [string]$BaseDir = '',
    [string]$TeacherUser = 'teacher',
    [switch]$Update
)

$ErrorActionPreference = 'Stop'
if (-not $BaseDir) {
    $scriptPath = $MyInvocation.MyCommand.Path
    if (-not $scriptPath) { throw 'Cannot locate the installer script.' }
    $BaseDir = Split-Path (Split-Path (Split-Path $scriptPath -Parent) -Parent) -Parent
}
$backend = Join-Path $BaseDir 'fcu-qduoj-fork'
$frontend = Join-Path $BaseDir 'fcu-qduoj-fe-fork'
$judge = Join-Path $BaseDir 'fcu-qduoj-judge-fork'
$envPath = Join-Path $backend 'fcu\.env'
$composePath = Join-Path $backend 'fcu\compose.yaml'
$composeArgs = @('--env-file', $envPath, '-f', $composePath)

function Assert-LastExit([string]$Action) {
    if ($LASTEXITCODE -ne 0) { throw "$Action failed (exit code $LASTEXITCODE)." }
}

function New-RandomHex([int]$ByteCount) {
    $bytes = New-Object byte[] $ByteCount
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
}

function Protect-PrivatePath([string]$Path) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $userGrant = '*' + $sid + ':(F)'
    $systemGrant = '*S-1-5-18:(F)'
    & icacls $Path '/inheritance:r' '/grant:r' $userGrant $systemGrant | Out-Null
    Assert-LastExit "Set private ACL on $Path"
}

function Get-ServiceId([string]$Service) {
    $ids = @(& docker compose @composeArgs ps -a -q $Service)
    Assert-LastExit "Find $Service container"
    return ($ids | Where-Object { $_ } | Select-Object -First 1)
}

function Test-ExistingDatabaseVolume {
    $volumes = @(& docker volume ls --format '{{.Name}}' --filter 'name=fcu-qduoj_postgres_data')
    Assert-LastExit 'List database volumes'
    return ($volumes -contains 'fcu-qduoj_postgres_data')
}

function Wait-Healthy([string]$Service, [int]$Seconds = 240) {
    $id = Get-ServiceId $Service
    if (-not $id) { throw "Container for $Service was not created." }
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $status = (& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $id)
        Assert-LastExit "Inspect $Service"
        if ($status -eq 'healthy') { return }
        if ($status -eq 'unhealthy' -or $status -eq 'exited' -or $status -eq 'dead') {
            throw "$Service is $status. Inspect docker compose logs $Service."
        }
        Start-Sleep -Seconds 3
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "$Service did not become healthy within $Seconds seconds."
}

function Ensure-Repository([string]$Path, [string]$Url) {
    if (-not (Test-Path $Path)) {
        & git clone $Url $Path
        Assert-LastExit "Clone $Url"
    }
    if (-not (Test-Path (Join-Path $Path '.git'))) {
        throw "Existing path is not a Git checkout: $Path"
    }
}

function Set-DetectedProxyPeer {
    # Docker Desktop may present the Windows host to nginx as a bridge address.
    # Observe one request from this host instead of trusting a whole bridge range.
    $backendId = Get-ServiceId 'oj-backend'
    $probeMarker = 'fcu-proxy-probe=' + (New-RandomHex 8)
    $probe = Invoke-WebRequest -Uri ('http://127.0.0.1:5482/classroom?' + $probeMarker) -UseBasicParsing -TimeoutSec 15
    if ($probe.StatusCode -ne 200) { throw 'The proxy peer probe did not return HTTP 200.' }

    $matchLine = $null
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        $logLines = @(& docker exec $backendId grep -F -- $probeMarker /data/log/nginx_access.log)
        if ($LASTEXITCODE -gt 1) { throw 'Could not read the nginx access log for the proxy peer probe.' }
        $matchLine = $logLines | Where-Object { $_ } | Select-Object -Last 1
        if ($matchLine) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $matchLine) { throw 'The unique proxy peer probe was not found in the nginx access log.' }
    $logMatch = [regex]::Match($matchLine, '^([^ ]+) - [^ ]+ \[')
    if (-not $logMatch.Success) { throw 'Could not parse the nginx proxy peer address.' }
    $peer = $logMatch.Groups[1].Value
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($peer, [ref]$parsed)) {
        throw 'The nginx proxy peer is not an IP address.'
    }
    $peerAddress = $parsed.ToString()
    if ($parsed.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) {
        $peerCidr = $peerAddress + '/32'
    } elseif ($parsed.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6) {
        $peerCidr = $peerAddress + '/128'
    } else {
        throw 'Unsupported nginx proxy peer address family.'
    }
    $cidrs = '127.0.0.1/32,::1/128,' + $peerCidr
    $lines = @(Get-Content -LiteralPath $envPath -Encoding UTF8)
    $found = $false
    $updated = @()
    foreach ($line in $lines) {
        if ($line -match '^FCU_TRUSTED_PROXY_CIDRS=') {
            if ($found) { throw 'Duplicate FCU_TRUSTED_PROXY_CIDRS entries in private environment file.' }
            $updated += 'FCU_TRUSTED_PROXY_CIDRS=' + $cidrs
            $found = $true
        } else {
            $updated += $line
        }
    }
    if (-not $found) { $updated += 'FCU_TRUSTED_PROXY_CIDRS=' + $cidrs }
    [IO.File]::WriteAllText($envPath, ($updated -join "`n") + "`n", ([Text.UTF8Encoding]::new($false)))
    Protect-PrivatePath $envPath
    & docker compose @composeArgs up -d --force-recreate --no-deps oj-backend
    Assert-LastExit 'Apply the exact proxy peer to backend'
    Wait-Healthy 'oj-backend' 300
    Write-Host "Trusted proxy peer restricted to $peerCidr (plus local loopback)."
}

function Backup-ExistingInstall {
    # A successful backup precedes any pull, build, migration, or container recreation.
    if (-not (Test-ExistingDatabaseVolume)) { return }

    $oldBackend = Get-ServiceId 'oj-backend'
    if (-not $oldBackend) {
        throw 'An existing PostgreSQL volume has no backend container. Back up the named volumes manually before upgrading.'
    }
    $backupRoot = Join-Path $BaseDir 'private-backups'
    New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
    Protect-PrivatePath $backupRoot
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupDir = Join-Path $backupRoot $stamp
    New-Item -ItemType Directory -Path $backupDir | Out-Null
    Protect-PrivatePath $backupDir
    Copy-Item -LiteralPath $envPath -Destination (Join-Path $backupDir 'fcu.env')
    Protect-PrivatePath (Join-Path $backupDir 'fcu.env')

    & docker compose @composeArgs up -d oj-postgres
    Assert-LastExit 'Start PostgreSQL for backup'
    Wait-Healthy 'oj-postgres'
    $wasRunning = & docker inspect --format '{{.State.Running}}' $oldBackend
    Assert-LastExit 'Inspect existing backend'
    if ($wasRunning -ne 'true') {
        & docker start $oldBackend | Out-Null
        Assert-LastExit 'Start existing backend for volume backup'
        Wait-Healthy 'oj-backend'
    }
    $postgres = Get-ServiceId 'oj-postgres'
    $temporary = '/tmp/fcu-predeploy-' + $stamp + '.dump'
    & docker exec $postgres sh -c ('PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U onlinejudge -Fc -f ' + $temporary + ' onlinejudge')
    Assert-LastExit 'Back up PostgreSQL'
    & docker cp "${postgres}:$temporary" (Join-Path $backupDir 'postgres.dump')
    Assert-LastExit 'Copy PostgreSQL backup'
    & docker exec $postgres rm -f $temporary
    Assert-LastExit 'Remove temporary database dump'
    & docker cp "${oldBackend}:/data" (Join-Path $backupDir 'data')
    Assert-LastExit 'Back up backend data and private test cases'
    Write-Host "Existing data backed up to $backupDir"
}

foreach ($command in @('git', 'npm', 'docker')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command is missing: $command"
    }
}
$dockerOs = & docker info --format '{{.OSType}}'
Assert-LastExit 'Check Docker engine mode'
if ($dockerOs.Trim() -ne 'linux') { throw 'Switch Docker Desktop to Linux containers.' }

New-Item -ItemType Directory -Force -Path $BaseDir | Out-Null
Ensure-Repository $backend 'https://github.com/EthanYangTW/OnlineJudge.git'
Ensure-Repository $frontend 'https://github.com/EthanYangTW/OnlineJudgeFE.git'
Ensure-Repository $judge 'https://github.com/EthanYangTW/JudgeServer.git'

if (-not (Test-Path $envPath)) {
    if (Test-ExistingDatabaseVolume) {
        throw 'Existing database volume found but fcu\.env is missing. Restore the original environment file before proceeding.'
    }
    if ($TeacherUser -notmatch '^[A-Za-z0-9_.-]{2,64}$') { throw 'Invalid teacher login name.' }
    $databasePassword = New-RandomHex 32
    $judgeToken = New-RandomHex 32
    $teacherPassword = New-RandomHex 32
    $lines = @(
        "FCU_DB_PASSWORD=$databasePassword",
        "FCU_JUDGE_TOKEN=$judgeToken",
        "FCU_TEACHER_USER=$TeacherUser",
        "FCU_TEACHER_PASSWORD=$teacherPassword",
        'FCU_BIND=127.0.0.1',
        'FCU_HTTP_PORT=5482',
        'FCU_TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128'
    )
    [IO.File]::WriteAllText($envPath, ($lines -join "`n") + "`n", (New-Object System.Text.ASCIIEncoding))
    Write-Host "Generated private credentials at $envPath (values are not printed)."
}
if (([IO.File]::ReadAllText($envPath)) -match 'REPLACE_WITH') {
    throw "Replace example credentials in $envPath before installing."
}
$envText = [IO.File]::ReadAllText($envPath)
if ($envText -notmatch '(?m)^FCU_BIND=127\.0\.0\.1\r?$' -or
    $envText -notmatch '(?m)^FCU_HTTP_PORT=5482\r?$') {
    throw 'This installer requires FCU_BIND=127.0.0.1 and FCU_HTTP_PORT=5482.'
}
Protect-PrivatePath $envPath

Push-Location $backend
try {
    & docker compose @composeArgs config --quiet
    Assert-LastExit 'Validate Compose configuration'
    Backup-ExistingInstall
    if ($Update) {
        foreach ($repo in @($backend, $frontend, $judge)) {
            & git -C $repo pull --ff-only
            Assert-LastExit "Update $repo"
        }
    }

    & git -C $judge submodule update --init --recursive
    Assert-LastExit 'Fetch the pinned Judger submodule'

    Push-Location $frontend
    try {
        $env:NODE_OPTIONS = '--openssl-legacy-provider'
        & npm install --legacy-peer-deps --no-audit
        Assert-LastExit 'Install frontend dependencies'
        & npm run build:dll
        Assert-LastExit 'Build frontend vendor bundle'
        & npm run build
        Assert-LastExit 'Build frontend'
        if (-not (Test-Path (Join-Path $frontend 'dist\index.html'))) {
            throw 'Frontend dist/index.html is missing.'
        }
    } finally {
        Pop-Location
    }

    & docker compose @composeArgs up --build -d
    Assert-LastExit 'Start classroom stack'
    foreach ($service in @('oj-postgres', 'oj-redis', 'oj-backend', 'oj-judge', 'oj-syntax')) {
        Wait-Healthy $service 300
    }
    Set-DetectedProxyPeer
    $response = Invoke-WebRequest -Uri 'http://127.0.0.1:5482/classroom' -UseBasicParsing -TimeoutSec 15
    if ($response.StatusCode -ne 200) { throw "Classroom returned HTTP $($response.StatusCode)." }
    Write-Host 'FCU classroom is healthy at http://127.0.0.1:5482/classroom'
    Write-Host 'Caddy has not been changed. Verify a C submission before switching port 80.'
} finally {
    Pop-Location
}
