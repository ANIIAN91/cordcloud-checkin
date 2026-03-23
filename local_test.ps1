param(
    [string]$Email,
    [string]$Passwd,
    [string]$SiteHost,
    [string]$Secret,
    [string]$Code,
    [string]$VerifyMethod,
    [switch]$TrustDevice,
    [switch]$InsecureSkipVerify
)

$ErrorActionPreference = "Stop"

$SiteHost = if ($SiteHost) { $SiteHost } else { "" }
$Secret = if ($Secret) { $Secret } else { "" }
$Code = if ($Code) { $Code } else { "" }
$VerifyMethod = if ($VerifyMethod) { $VerifyMethod } else { "" }

if ($Email) { $env:INPUT_EMAIL = $Email } else { Remove-Item Env:INPUT_EMAIL -ErrorAction SilentlyContinue }
if ($Passwd) { $env:INPUT_PASSWD = $Passwd } else { Remove-Item Env:INPUT_PASSWD -ErrorAction SilentlyContinue }
if ($SiteHost) { $env:INPUT_HOST = $SiteHost } else { Remove-Item Env:INPUT_HOST -ErrorAction SilentlyContinue }
if ($Secret) { $env:INPUT_SECRET = $Secret } else { Remove-Item Env:INPUT_SECRET -ErrorAction SilentlyContinue }
if ($Code) { $env:INPUT_CODE = $Code } else { Remove-Item Env:INPUT_CODE -ErrorAction SilentlyContinue }
if ($VerifyMethod) { $env:INPUT_VERIFY_METHOD = $VerifyMethod } else { Remove-Item Env:INPUT_VERIFY_METHOD -ErrorAction SilentlyContinue }
if ($TrustDevice.IsPresent) { $env:INPUT_TRUST_DEVICE = 'true' } else { Remove-Item Env:INPUT_TRUST_DEVICE -ErrorAction SilentlyContinue }
if ($InsecureSkipVerify.IsPresent) { $env:INPUT_INSECURE_SKIP_VERIFY = 'true' } else { Remove-Item Env:INPUT_INSECURE_SKIP_VERIFY -ErrorAction SilentlyContinue }

& "D:\CLI\Tiktok\cordcloud-action\.conda\cordcloud-test\python.exe" "D:\CLI\Tiktok\cordcloud-action\main.py"
