function Install-Pitwall {
    $installDir = $env:PITWALL_DIR
    if ([string]::IsNullOrWhiteSpace($installDir)) {
        $installDir = Join-Path $env:USERPROFILE "f1-pitwall"
    }
    $installDir = [System.IO.Path]::GetFullPath($installDir)

    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $uvCommand) {
        $wingetCommand = Get-Command winget -ErrorAction SilentlyContinue
        if ($null -ne $wingetCommand) {
            & $wingetCommand.Source install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
            if ($LASTEXITCODE -ne 0) {
                Write-Host "uv installation failed."
                return
            }
        } else {
            & powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
            if ($LASTEXITCODE -ne 0) {
                Write-Host "uv installation failed."
                return
            }
        }
    }

    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if ($null -eq $gitCommand) {
        $wingetCommand = Get-Command winget -ErrorAction SilentlyContinue
        if ($null -ne $wingetCommand) {
            & $wingetCommand.Source install --id Git.Git -e --accept-source-agreements --accept-package-agreements
            if ($LASTEXITCODE -ne 0) {
                Write-Host "Git installation failed."
                return
            }
        } else {
            Write-Host "Install Git from https://git-scm.com/download/win, then run this installer again."
            return
        }
    }

    $pathEntries = @(
        [System.Environment]::GetEnvironmentVariable("Path", "Machine")
        [System.Environment]::GetEnvironmentVariable("Path", "User")
        (Join-Path $env:USERPROFILE ".local\bin")
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links")
        (Join-Path $env:ProgramFiles "Git\cmd")
    )
    $env:Path = ($pathEntries | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ";"
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if ($null -eq $uvCommand -or $null -eq $gitCommand) {
        Write-Host "Close this window, open a new PowerShell, and run the same command again."
        return
    }
    $uv = $uvCommand.Source
    $git = $gitCommand.Source

    if (Test-Path -LiteralPath $installDir) {
        $topLevel = & $git -C $installDir rev-parse --show-toplevel 2>$null
        $gitCode = $LASTEXITCODE
        if (
            $gitCode -ne 0 -or
            -not (Test-Path -LiteralPath (Join-Path $installDir "pyproject.toml")) -or
            -not (Select-String -Path (Join-Path $installDir 'pyproject.toml') -Pattern '^name = "pitwall"' -Quiet)
        ) {
            Write-Host "PITWALL_DIR points to a folder that is not a Pitwall git clone: $installDir"
            Write-Host "Set PITWALL_DIR to another folder or move this folder first."
            return
        }
        $topLevelPath = [System.IO.Path]::GetFullPath(([string]$topLevel).Trim())
        if ($topLevelPath.TrimEnd("\") -ne $installDir.TrimEnd("\")) {
            Write-Host "PITWALL_DIR points to a folder that is not a Pitwall git clone: $installDir"
            Write-Host "Set PITWALL_DIR to another folder or move this folder first."
            return
        }

        $changes = & $git -C $installDir status --porcelain --untracked-files=no
        $gitCode = $LASTEXITCODE
        if ($gitCode -ne 0) {
            Write-Host "Could not check the Pitwall folder. Pitwall did not update."
            return
        }
        if (@($changes).Count -gt 0) {
            Write-Host "Tracked files have local changes, so the installer skips git pull. uv sync still runs."
        } else {
            & $git -C $installDir pull --ff-only
            if ($LASTEXITCODE -ne 0) {
                Write-Host "git pull failed. Pitwall did not update."
                return
            }
        }
    } else {
        $parentDir = Split-Path -Parent $installDir
        New-Item -ItemType Directory -Force -Path $parentDir | Out-Null
        & $git clone https://github.com/artsworks/f1-pitwall.git $installDir
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Git could not download Pitwall."
            return
        }
    }

    Push-Location $installDir
    & $uv sync
    $syncCode = $LASTEXITCODE
    Pop-Location
    if ($syncCode -ne 0) {
        Write-Host "uv sync failed. Pitwall did not finish installing."
        return
    }

    Write-Host "Where does F1 26 run?"
    Write-Host "  1) On this PC"
    Write-Host "  2) On another PC or console on this network"
    $whereGameRuns = Read-Host "Choice [1]"
    if ([string]::IsNullOrWhiteSpace($whereGameRuns)) {
        $whereGameRuns = "1"
    }
    if ($whereGameRuns -ne "1" -and $whereGameRuns -ne "2") {
        Write-Host "Enter 1 or 2, then run the installer again."
        return
    }

    $voiceAnswer = Read-Host "Download the natural voice (about 60 MB)? [Y/n]"
    if ($voiceAnswer -notmatch "^(n|N)$") {
        if ((Test-Path (Join-Path $env:SystemRoot 'System32\vcruntime140_1.dll')) -or (Test-Path (Join-Path $env:SystemRoot 'Sysnative\vcruntime140_1.dll'))) {
            Push-Location $installDir
            & $uv run pitwall voice get
            $voiceCode = $LASTEXITCODE
            Pop-Location
            if ($voiceCode -ne 0) {
                Write-Host "The natural voice did not download. You can run pitwall voice get later."
            }
        } else {
            Write-Host "The natural voice needs the Microsoft Visual C++ Redistributable."
            Write-Host "Install it from https://aka.ms/vs/17/release/vc_redist.x64.exe, then run this installer again."
        }
    }

    $binDir = Join-Path $env:USERPROFILE ".local\bin"
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $cmdPath = Join-Path $binDir "pitwall.cmd"
    $cmdContent = "@echo off`r`nsetlocal`r`nset `"PITWALL_HOME=$installDir`"`r`n`"$uv`" run --project `"$installDir`" python -m pitwall %*`r`nexit /b %ERRORLEVEL%`r`n"
    [System.IO.File]::WriteAllText($cmdPath, $cmdContent, [System.Text.UTF8Encoding]::new($false))
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $normalizedBin = $binDir.TrimEnd("\")
    $userEntries = @()
    if (-not [string]::IsNullOrWhiteSpace($userPath)) {
        $userEntries = @($userPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    }
    $hasBinInUserPath = $false
    foreach ($entry in $userEntries) {
        if ($entry.Trim().TrimEnd("\") -ieq $normalizedBin) {
            $hasBinInUserPath = $true
            break
        }
    }
    if (-not $hasBinInUserPath) {
        $newUserPath = ($userEntries + $binDir) -join ";"
        [System.Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
    }
    if (-not (($env:Path -split ";") | Where-Object { $_.Trim().TrimEnd("\") -ieq $normalizedBin })) {
        $env:Path = "$binDir;$env:Path"
    }

    $escapedUv = $uv.Replace("'", "''")
    $startArguments = '-NoExit -NoProfile -ExecutionPolicy Bypass -Command "& ''' + $escapedUv + ''' run pitwall start"'
    # Use python -m so uv sync can replace pitwall.exe without the shortcut holding it open.
    $updateArguments = '-NoExit -NoProfile -ExecutionPolicy Bypass -Command "& ''' + $escapedUv + ''' run python -m pitwall update"'
    $powerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $iconPath = Join-Path $installDir "docs\favicon.ico"
    $startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Pitwall"
    $desktop = Join-Path $env:USERPROFILE "Desktop"
    New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
    New-Item -ItemType Directory -Force -Path $desktop | Out-Null
    $shell = New-Object -ComObject WScript.Shell

    $shortcut = $shell.CreateShortcut((Join-Path $startMenu "Pitwall.lnk"))
    $shortcut.TargetPath = $powerShell
    $shortcut.Arguments = $startArguments
    $shortcut.WorkingDirectory = $installDir
    $shortcut.IconLocation = $iconPath
    $shortcut.Save()
    $shortcut = $shell.CreateShortcut((Join-Path $startMenu "Pitwall Update.lnk"))
    $shortcut.TargetPath = $powerShell
    $shortcut.Arguments = $updateArguments
    $shortcut.WorkingDirectory = $installDir
    $shortcut.IconLocation = $iconPath
    $shortcut.Save()

    $shortcut = $shell.CreateShortcut((Join-Path $desktop "Pitwall.lnk"))
    $shortcut.TargetPath = $powerShell
    $shortcut.Arguments = $startArguments
    $shortcut.WorkingDirectory = $installDir
    $shortcut.IconLocation = $iconPath
    $shortcut.Save()
    $shortcut = $shell.CreateShortcut((Join-Path $desktop "Pitwall Update.lnk"))
    $shortcut.TargetPath = $powerShell
    $shortcut.Arguments = $updateArguments
    $shortcut.WorkingDirectory = $installDir
    $shortcut.IconLocation = $iconPath
    $shortcut.Save()

    Push-Location $installDir
    & $uv run pitwall doctor
    $doctorCode = $LASTEXITCODE
    Pop-Location
    if ($doctorCode -ne 0) {
        Write-Host "Pitwall doctor returned an error."
    }
    Write-Host "0 datagrams is normal until you are on track."

    Write-Host ""
    Write-Host "Pitwall is installed in $installDir."
    if ($whereGameRuns -eq "1") {
        Write-Host "F1 26 UDP IP Address: 127.0.0.1"
        Write-Host "Other F1 26 Telemetry Settings: UDP Telemetry On, Broadcast Off, Port 20777, Send Rate 30 Hz, Format 2026."
        Write-Host "Open http://localhost:8000 on the second monitor."
        Write-Host "Guide: https://pitwall.arthurku.com/install.html#windows"
    } else {
        $lanConfig = Get-NetIPConfiguration -ErrorAction SilentlyContinue | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } | Select-Object -First 1
        $lanIp = "the LAN IPv4 address shown by Get-NetIPConfiguration"
        if ($null -ne $lanConfig -and $lanConfig.IPv4Address.Count -gt 0) {
            $lanIp = $lanConfig.IPv4Address[0].IPAddress
        }
        Write-Host "F1 26 UDP IP Address: $lanIp"
        Write-Host "Other F1 26 Telemetry Settings: UDP Telemetry On, Broadcast Off, Port 20777, Send Rate 30 Hz, Format 2026."
        Write-Host 'In an admin PowerShell, run: netsh advfirewall firewall add rule name="pitwall udp" dir=in action=allow protocol=UDP localport=20777'
        Write-Host 'In an admin PowerShell, run: netsh advfirewall firewall add rule name="pitwall tcp" dir=in action=allow protocol=TCP localport=8000'
        Write-Host "Open http://${lanIp}:8000 on another device. Enter the 4-digit PIN Pitwall prints at start."
        Write-Host "Guide: https://pitwall.arthurku.com/install.html#windows-lan"
    }
    Write-Host "Start Pitwall from the Pitwall shortcut or run: pitwall start"
    Write-Host "To update, use Pitwall Update or run: pitwall update"
}
Install-Pitwall
