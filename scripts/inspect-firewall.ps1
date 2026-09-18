# Read-only LanDrop firewall inspection. This script does not change any rule.
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [string]$ProgramPath = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Test-PrivateProfile {
    param([string]$Profile)
    return $Profile -match 'Private|Any|All'
}

function Get-PortScope {
    param([string]$LocalPort, [int]$TargetPort)
    if ([string]::IsNullOrWhiteSpace($LocalPort) -or $LocalPort -in @('Any', '*')) {
        return 'broad'
    }
    foreach ($item in ($LocalPort -split ',')) {
        $value = $item.Trim()
        if ($value -eq [string]$TargetPort) { return 'exact' }
        if ($value -match '^(\d+)\s*[-:]\s*(\d+)$') {
            if ([int]$Matches[1] -le $TargetPort -and $TargetPort -le [int]$Matches[2]) {
                return 'broad'
            }
        }
    }
    return 'none'
}

$normalizedProgram = if ($ProgramPath) {
    [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($ProgramPath))
} else { '' }

$profiles = Get-NetFirewallProfile |
    ForEach-Object {
        [PSCustomObject]@{
            Name = [string]$_.Name
            Enabled = [string]$_.Enabled
            DefaultInboundAction = [string]$_.DefaultInboundAction
            DefaultOutboundAction = [string]$_.DefaultOutboundAction
        }
    }

$enabledRules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Enabled True -Direction Inbound)
$allApplications = @($enabledRules | Get-NetFirewallApplicationFilter)
$allPorts = @($enabledRules | Get-NetFirewallPortFilter)
$allAddresses = @($enabledRules | Get-NetFirewallAddressFilter)
$allInterfaces = @($enabledRules | Get-NetFirewallInterfaceFilter)

function Group-ByInstanceId {
    param([object[]]$Items)
    $table = @{}
    foreach ($item in $Items) {
        $key = [string]$item.InstanceID
        if (-not $table.ContainsKey($key)) {
            $table[$key] = [System.Collections.ArrayList]::new()
        }
        [void]$table[$key].Add($item)
    }
    return ,$table
}

$applicationsById = Group-ByInstanceId $allApplications
$portsById = Group-ByInstanceId $allPorts
$addressesById = Group-ByInstanceId $allAddresses
$interfacesById = Group-ByInstanceId $allInterfaces

$rules = $enabledRules | ForEach-Object {
        $rule = $_
        if (-not (Test-PrivateProfile ([string]$rule.Profile))) { return }
        $key = [string]$rule.InstanceID
        $applications = @($applicationsById[$key])
        $ports = @($portsById[$key])
        $addresses = @($addressesById[$key])
        $interfaces = @($interfacesById[$key])
        if ($applications.Count -eq 0) { $applications = @($null) }
        if ($ports.Count -eq 0) { $ports = @($null) }
        foreach ($application in $applications) {
            foreach ($portFilter in $ports) {
                $program = if ($application) { [string]$application.Program } else { 'Any' }
                $protocol = if ($portFilter) { [string]$portFilter.Protocol } else { 'Any' }
                $localPort = if ($portFilter) { [string]$portFilter.LocalPort } else { 'Any' }
                if ($protocol -notin @('TCP', '6', 'Any')) { continue }
                $portScope = Get-PortScope $localPort $Port
                $programMatch = $false
                if ($normalizedProgram -and $program -notin @('Any', '*', '')) {
                    try {
                        $expanded = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($program))
                        $programMatch = $expanded -ieq $normalizedProgram
                    } catch { $programMatch = $false }
                }
                $programAny = $program -in @('Any', '*', '')
                if (-not $programAny -and -not $programMatch) { continue }
                if ($portScope -eq 'none') { continue }
                [PSCustomObject]@{
                    Name = [string]$rule.Name
                    DisplayName = [string]$rule.DisplayName
                    Enabled = [string]$rule.Enabled
                    Profile = [string]$rule.Profile
                    Direction = [string]$rule.Direction
                    Action = [string]$rule.Action
                    Program = $program
                    ProgramMatch = $programMatch
                    Protocol = $protocol
                    LocalPort = $localPort
                    PortScope = $portScope
                    LocalAddress = @($addresses | ForEach-Object { [string]$_.LocalAddress })
                    RemoteAddress = @($addresses | ForEach-Object { [string]$_.RemoteAddress })
                    InterfaceAlias = @($interfaces | ForEach-Object { [string]$_.InterfaceAlias })
                }
            }
        }
    }

[PSCustomObject]@{
    TargetPort = $Port
    TargetProgram = $normalizedProgram
    Profiles = @($profiles)
    Rules = @($rules)
} | ConvertTo-Json -Depth 6
