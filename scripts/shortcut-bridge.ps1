param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Read', 'Write')]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$RequestPath
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace LanDropShortcutBridge
{
    [ComImport]
    [Guid("00021401-0000-0000-C000-000000000046")]
    internal class ShellLink { }

    [ComImport]
    [Guid("0000010B-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IPersistFile
    {
        [PreserveSig] int GetClassID(out Guid classId);
        [PreserveSig] int IsDirty();
        [PreserveSig] int Load([MarshalAs(UnmanagedType.LPWStr)] string fileName, uint mode);
        [PreserveSig] int Save([MarshalAs(UnmanagedType.LPWStr)] string fileName, [MarshalAs(UnmanagedType.Bool)] bool remember);
        [PreserveSig] int SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string fileName);
        [PreserveSig] int GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string fileName);
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    internal struct PropertyKey
    {
        internal Guid formatId;
        internal uint propertyId;
        internal PropertyKey(Guid formatId, uint propertyId)
        {
            this.formatId = formatId;
            this.propertyId = propertyId;
        }
    }

    [StructLayout(LayoutKind.Explicit, Size = 24)]
    internal struct PropVariant
    {
        [FieldOffset(0)] internal ushort valueType;
        [FieldOffset(8)] internal IntPtr pointerValue;
    }

    [ComImport]
    [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IPropertyStore
    {
        [PreserveSig] int GetCount(out uint propertyCount);
        [PreserveSig] int GetAt(uint propertyIndex, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }

    public static class ShortcutIdentity
    {
        private const ushort VT_EMPTY = 0;
        private const ushort VT_LPWSTR = 31;
        private static readonly PropertyKey AppUserModelIdKey =
            new PropertyKey(new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);

        [DllImport("ole32.dll")]
        private static extern int PropVariantClear(ref PropVariant value);

        private static void Check(int result)
        {
            if (result < 0) Marshal.ThrowExceptionForHR(result, new IntPtr(-1));
        }

        public static string Read(string path)
        {
            object link = new ShellLink();
            try
            {
                IPersistFile persist = (IPersistFile)link;
                Check(persist.Load(path, 0));
                IPropertyStore store = (IPropertyStore)link;
                PropertyKey key = AppUserModelIdKey;
                PropVariant value;
                Check(store.GetValue(ref key, out value));
                try
                {
                    if (value.valueType == VT_EMPTY) return null;
                    if (value.valueType != VT_LPWSTR || value.pointerValue == IntPtr.Zero)
                        throw new InvalidOperationException("Shortcut AppUserModelID has an unsupported type.");
                    return Marshal.PtrToStringUni(value.pointerValue);
                }
                finally { PropVariantClear(ref value); }
            }
            finally { Marshal.FinalReleaseComObject(link); }
        }

        public static void Write(string path, string appUserModelId)
        {
            object link = new ShellLink();
            try
            {
                IPersistFile persist = (IPersistFile)link;
                Check(persist.Load(path, 2));
                IPropertyStore store = (IPropertyStore)link;
                PropertyKey key = AppUserModelIdKey;
                PropVariant value = new PropVariant();
                value.valueType = VT_LPWSTR;
                value.pointerValue = Marshal.StringToCoTaskMemUni(appUserModelId);
                try
                {
                    Check(store.SetValue(ref key, ref value));
                    Check(store.Commit());
                    Check(persist.Save(path, true));
                }
                finally { PropVariantClear(ref value); }
            }
            finally { Marshal.FinalReleaseComObject(link); }
        }
    }
}
'@

function Write-Result {
    param([hashtable]$Value)
    $Value | ConvertTo-Json -Depth 6 -Compress
}

function Assert-ExactProperties {
    param([object]$Value, [string[]]$Expected)
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $wanted = @($Expected | Sort-Object)
    if (($actual -join "`n") -ne ($wanted -join "`n")) {
        throw 'Shortcut request contains missing or unknown fields.'
    }
}

function Resolve-LanDropShortcutPath {
    param([object]$Value)
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value)) {
        throw 'Shortcut path must be a non-empty string.'
    }
    $resolved = [IO.Path]::GetFullPath($Value)
    $fullyQualified = (
        $resolved -match '^[A-Za-z]:[\\/]' -or
        $resolved -match '^\\\\[^\\]+\\[^\\]+(?:\\|$)'
    )
    if (-not $fullyQualified -or [IO.Path]::GetFileName($resolved) -cne 'LanDrop.lnk') {
        throw 'Shortcut path is not a managed LanDrop shortcut.'
    }
    return $resolved
}

$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell

if ($Action -eq 'Read') {
    Assert-ExactProperties $request @('path')
    $path = Resolve-LanDropShortcutPath $request.path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Write-Result @{ ok = $true; action = 'Read'; exists = $false; readable = $false; shortcut = $null }
        exit 0
    }
    try {
        $shortcut = $shell.CreateShortcut($path)
        $definition = @{
            path = $path
            target = [string]$shortcut.TargetPath
            arguments = [string]$shortcut.Arguments
            workingDirectory = [string]$shortcut.WorkingDirectory
            description = [string]$shortcut.Description
            iconLocation = [string]$shortcut.IconLocation
            appUserModelId = [LanDropShortcutBridge.ShortcutIdentity]::Read($path)
        }
    }
    catch {
        Write-Result @{ ok = $true; action = 'Read'; exists = $true; readable = $false; shortcut = $null }
        exit 0
    }
    Write-Result @{ ok = $true; action = 'Read'; exists = $true; readable = $true; shortcut = $definition }
    exit 0
}

Assert-ExactProperties $request @(
    'path', 'target', 'arguments', 'workingDirectory', 'description', 'iconLocation', 'appUserModelId'
)
$path = Resolve-LanDropShortcutPath $request.path
foreach ($name in @('target', 'arguments', 'workingDirectory', 'description', 'iconLocation', 'appUserModelId')) {
    if ($request.$name -isnot [string]) { throw "Shortcut field $name must be a string." }
}
if ([IO.Path]::GetFileName($request.target) -cne 'LanDrop.exe') { throw 'Shortcut target is invalid.' }
if ($request.arguments -cne '') { throw 'Shortcut arguments must be empty.' }
if ($request.appUserModelId -cne 'LanDrop.Desktop') { throw 'Shortcut AppUserModelID is invalid.' }
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = $request.target
$shortcut.Arguments = $request.arguments
$shortcut.WorkingDirectory = $request.workingDirectory
$shortcut.Description = $request.description
$shortcut.IconLocation = $request.iconLocation
$shortcut.Save()
[LanDropShortcutBridge.ShortcutIdentity]::Write($path, $request.appUserModelId)
if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Shortcut was not created.' }
if ([LanDropShortcutBridge.ShortcutIdentity]::Read($path) -cne $request.appUserModelId) {
    throw 'Shortcut AppUserModelID readback mismatch.'
}
Write-Result @{ ok = $true; action = 'Write'; exists = $true; path = $path }
