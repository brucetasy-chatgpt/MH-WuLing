param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "result", "next", "status", "reset")]
    [string]$Command,

    [ValidateSet("booked", "failed", "skipped")]
    [string]$ResultStatus,

    [string]$ResultNote = "",

    [string]$OrderFile,
    [string]$LogDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot

function Resolve-ProjectPath {
    param(
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$DefaultRelativePath
    )

    $candidate = if ([string]::IsNullOrWhiteSpace($Path)) {
        Join-Path $projectRoot $DefaultRelativePath
    } elseif ([System.IO.Path]::IsPathRooted($Path)) {
        $Path
    } else {
        Join-Path $projectRoot $Path
    }

    return [System.IO.Path]::GetFullPath($candidate)
}

$OrderFile = Resolve-ProjectPath -Path $OrderFile -DefaultRelativePath "PS\ORDER_SEQ.INI"
$LogDir = Resolve-ProjectPath -Path $LogDir -DefaultRelativePath "LOG"

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -Path $LogDir -ItemType Directory -Force | Out-Null
}

$statePath = Join-Path $LogDir "order-seq-state.json"
$stepLogPath = Join-Path $LogDir ("order-seq-steps-" + (Get-Date -Format "yyyyMMdd") + ".log")

function Write-StepLog {
    param(
        [string]$Action,
        [string]$Message,
        [hashtable]$Data = @{}
    )

    $entry = [ordered]@{
        time    = (Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff")
        action  = $Action
        message = $Message
        data    = $Data
    } | ConvertTo-Json -Compress -Depth 8
    Add-Content -LiteralPath $stepLogPath -Value $entry -Encoding UTF8
}

function Parse-OrderItems {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Order file not found: $Path"
    }

    $lines = Get-Content -LiteralPath $Path
    $site = ""
    $group = ""
    $items = New-Object System.Collections.Generic.List[object]

    foreach ($line in $lines) {
        $trim = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trim)) { continue }

        if ($trim.StartsWith("[武陵國民賓館")) {
            $site = "whotel"
            $group = ""
            continue
        }
        if ($trim.StartsWith("[露營區")) {
            $site = "camping"
            $group = ""
            continue
        }

        if ($trim -match "^一、") { $group = "木屋"; continue }
        if ($trim -match "^二、") { $group = "可愛屋"; continue }
        if ($trim -match "^三、") { $group = "營位"; continue }

        if ($trim -match "^\d+\.\s*(.+?)：") {
            $name = $Matches[1].Trim()
            $items.Add([pscustomobject]@{
                order = $items.Count + 1
                site  = $site
                group = $group
                name  = $name
                raw   = $trim
            })
        }
    }

    if ($items.Count -eq 0) {
        throw "No booking items parsed from: $Path"
    }

    return $items
}

function New-State {
    param([array]$Queue)
    return [ordered]@{
        version            = 1
        createdAt          = (Get-Date -Format "s")
        updatedAt          = (Get-Date -Format "s")
        pausedAfterBooked  = $false
        currentIndex       = 0
        queue              = @($Queue | ForEach-Object {
            [ordered]@{
                order      = $_.order
                site       = $_.site
                group      = $_.group
                name       = $_.name
                raw        = $_.raw
                status     = "pending"
                updatedAt  = $null
                resultNote = $null
            }
        })
    }
}

function Load-State {
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json -AsHashtable
}

function Save-State {
    param([hashtable]$State)
    $State.updatedAt = (Get-Date -Format "s")
    $State | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $statePath -Encoding UTF8
}

function Get-NextPendingIndex {
    param([hashtable]$State)
    for ($i = 0; $i -lt $State.queue.Count; $i++) {
        if ($State.queue[$i].status -eq "pending") {
            return $i
        }
    }
    return -1
}

function Show-CurrentTarget {
    param([hashtable]$State)
    $idx = [int]$State.currentIndex
    if ($idx -lt 0 -or $idx -ge $State.queue.Count) {
        Write-Output "No current target."
        return
    }
    $item = $State.queue[$idx]
    Write-Output ("Current target: #{0} [{1}/{2}] {3}" -f $item.order, $item.site, $item.group, $item.raw)
}

function Dispatch-NextPending {
    param([hashtable]$State, [string]$Reason)

    $idx = Get-NextPendingIndex -State $State
    if ($idx -eq -1) {
        Write-StepLog -Action "queue_complete" -Message "All items finished" -Data @{ reason = $Reason }
        Save-State -State $State
        Write-Output "Queue complete. No pending items."
        return
    }

    $State.currentIndex = $idx
    $item = $State.queue[$idx]
    if ($item.status -eq "pending") {
        $item.status = "dispatched"
        $item.updatedAt = (Get-Date -Format "s")
        $State.queue[$idx] = $item
    }

    Save-State -State $State
    Write-StepLog -Action "dispatch" -Message "Target dispatched" -Data @{
        reason = $Reason
        index  = $idx
        order  = $item.order
        site   = $item.site
        group  = $item.group
        name   = $item.name
    }

    Show-CurrentTarget -State $State
    Write-Output "After booking attempt, run:"
    Write-Output "  .\Run-OrderSeq.ps1 -Command result -ResultStatus booked|failed|skipped -ResultNote '...'"
}

switch ($Command) {
    "reset" {
        if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath -Force }
        Write-StepLog -Action "reset" -Message "State reset"
        Write-Output "State reset done."
        break
    }

    "start" {
        $queue = Parse-OrderItems -Path $OrderFile
        $state = New-State -Queue $queue
        Save-State -State $state
        Write-StepLog -Action "start" -Message "Queue initialized" -Data @{ count = $queue.Count }
        Dispatch-NextPending -State $state -Reason "start"
        break
    }

    "status" {
        $state = Load-State
        if ($null -eq $state) {
            Write-Output "No state found. Run start first."
            break
        }
        Write-Output ("pausedAfterBooked: {0}" -f $state.pausedAfterBooked)
        Show-CurrentTarget -State $state
        $summary = $state.queue | Group-Object status | ForEach-Object { "{0}:{1}" -f $_.Name, $_.Count }
        Write-Output ("Summary: " + ($summary -join ", "))
        break
    }

    "result" {
        $state = Load-State
        if ($null -eq $state) {
            throw "No state found. Run start first."
        }
        if ([string]::IsNullOrWhiteSpace($ResultStatus)) {
            throw "result command requires -ResultStatus booked|failed|skipped"
        }

        $idx = [int]$state.currentIndex
        if ($idx -lt 0 -or $idx -ge $state.queue.Count) {
            throw "Invalid current index in state."
        }

        $item = $state.queue[$idx]
        $item.status = $ResultStatus
        $item.updatedAt = (Get-Date -Format "s")
        $item.resultNote = $ResultNote
        $state.queue[$idx] = $item

        Write-StepLog -Action "result" -Message "Booking result recorded" -Data @{
            index  = $idx
            order  = $item.order
            site   = $item.site
            group  = $item.group
            name   = $item.name
            status = $ResultStatus
            note   = $ResultNote
        }

        if ($ResultStatus -eq "booked") {
            $state.pausedAfterBooked = $true
            Save-State -State $state
            Write-StepLog -Action "paused" -Message "Paused after booked; waiting for next/continue"
            Write-Output "Booked recorded. Paused."
            Write-Output "To continue queue, run: .\Run-OrderSeq.ps1 -Command next"
            break
        }

        $state.pausedAfterBooked = $false
        Save-State -State $state
        Dispatch-NextPending -State $state -Reason "result:$ResultStatus"
        break
    }

    "next" {
        $state = Load-State
        if ($null -eq $state) {
            throw "No state found. Run start first."
        }
        if (-not $state.pausedAfterBooked) {
            Write-StepLog -Action "next_no_pause" -Message "Next requested while not paused"
            throw "Queue is not paused. Record the current result with booked, failed, or skipped first."
        }

        Write-StepLog -Action "continue" -Message "Continue received (next/continue)"

        $state.pausedAfterBooked = $false
        Save-State -State $state
        Dispatch-NextPending -State $state -Reason "next"
        break
    }
}
