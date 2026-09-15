# Read-only LanDrop firewall inspection. This script does not change any rule.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$profiles = Get-NetFirewallProfile |
    ForEach-Object {
        [PSCustomObject]@{
            Name = [string]$_.Name
            Enabled = [string]$_.Enabled
            DefaultInboundAction = [string]$_.DefaultInboundAction
            DefaultOutboundAction = [string]$_.DefaultOutboundAction
        }
    }

$rules = Get-NetFirewallRule |
    Where-Object {
        $_.DisplayName -like '*Python*' -or $_.Name -like '*Python*'
    } |
    ForEach-Object {
        $rule = $_
        $application = $rule | Get-NetFirewallApplicationFilter
        $address = $rule | Get-NetFirewallAddressFilter
        $interface = $rule | Get-NetFirewallInterfaceFilter
        $ports = @($rule | Get-NetFirewallPortFilter)
        if ($ports.Count -eq 0) {
            $ports = @($null)
        }
        foreach ($port in $ports) {
            [PSCustomObject]@{
                Name = [string]$rule.Name
                DisplayName = [string]$rule.DisplayName
                Enabled = [string]$rule.Enabled
                Profile = [string]$rule.Profile
                Direction = [string]$rule.Direction
                Action = [string]$rule.Action
                Program = [string]$application.Program
                Protocol = if ($port) { [string]$port.Protocol } else { '' }
                LocalPort = if ($port) { [string]$port.LocalPort } else { '' }
                RemotePort = if ($port) { [string]$port.RemotePort } else { '' }
                LocalAddress = [string]$address.LocalAddress
                RemoteAddress = [string]$address.RemoteAddress
                InterfaceAlias = [string]$interface.InterfaceAlias
            }
        }
    }

[PSCustomObject]@{
    Profiles = @($profiles)
    Rules = @($rules)
} | ConvertTo-Json -Depth 4
