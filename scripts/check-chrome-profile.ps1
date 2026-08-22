<#
  VOD.RIP - verificacao segura do Chrome do usuario (rodar ANTES de qualquer acesso).

  Uso:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check-chrome-profile.ps1

  Garante:
    1. Nenhum processo Chrome com user-data-dir alternativo (junction / perfil
       copiado) nem com --remote-debugging-port. CDP em perfil alternativo
       REVOGA a conta Google (mecanismo anti-sequestro do Chrome; irreversivel).
    2. Perfil ativo = Default com conta Google associada
       (user_name no Local State + account_info no Preferences).
    3. Login Twitch presente (cookie auth-token) quando o Chrome esta fechado.

  Saida: exit 0 = PERFIL_OK (pode acessar), 1 = PERFIL_ANORMAL (NAO acessar).
  ASCII-only (PowerShell 5.1 le arquivo como ANSI).
#>
$ErrorActionPreference = 'SilentlyContinue'
$problems = @()
$notes = @()

$ud  = Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data'
$def = Join-Path $ud 'Default'

Write-Host "== VOD.RIP check-chrome-profile ==" -ForegroundColor Cyan
Write-Host ("Perfil base: " + $ud)

# --- 1) Processos Chrome: user-data-dir alternativo / CDP ---
$procs = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue)
Write-Host ("Processos Chrome: " + $procs.Count)
if ($procs.Count -gt 0) {
  $esc = [regex]::Escape($ud)
  # consultgpt MCP runs its own headless Chrome with disposable profiles under
  # %USERPROFILE%\.consultgpt\chatgpt_profiles\ — never touches the user's
  # profile; excluding them avoids false PERFIL_ANORMAL every time MCP is hot.
  $escCp = [regex]::Escape((Join-Path $env:USERPROFILE '.consultgpt'))
  $alt = @($procs | Where-Object { $_.CommandLine -match '--user-data-dir=' -and $_.CommandLine -notmatch $esc -and $_.CommandLine -notmatch $escCp })
  $cdp = @($procs | Where-Object { $_.CommandLine -match '--remote-debugging-port' })
  $cdpReal = @($cdp | Where-Object { $_.CommandLine -match $esc -and $_.CommandLine -notmatch $escCp })
  if ($alt.Count -gt 0) { $problems += "$($alt.Count) processo(s) com user-data-dir ALTERNATIVO (junction/perfil copiado)" }
  if ($cdpReal.Count -gt 0) { $notes += "$($cdpReal.Count) processo(s) com CDP no perfil REAL - esperado durante o auto-install da extensao (scripts/cookie_extension_auto_install.ps1); aguardar concluir" }
  if ($cdp.Count -gt $cdpReal.Count) { $problems += "$($cdp.Count - $cdpReal.Count) processo(s) com --remote-debugging-port fora do perfil real" }
}

# --- 2) Conta Google no perfil Default ---
$lsPath   = Join-Path $ud 'Local State'
$prefPath = Join-Path $def 'Preferences'
if (Test-Path $lsPath) {
  try {
    $ls = Get-Content $lsPath -Raw | ConvertFrom-Json
    $d = $ls.profile.info_cache.Default
    if ($d) {
      $acc = if ($d.user_name) { $d.user_name } else { '(vazia)' }
      Write-Host ("Perfil: " + $d.name + " | conta Google: " + $acc)
      if (-not $d.user_name) { $problems += "Conta Google nao associada ao perfil (user_name vazio)" }
    } else {
      $problems += "Perfil Default ausente do info_cache"
    }
  } catch { $problems += "Local State ilegivel: $($_.Exception.Message)" }
} else { $problems += "Local State nao encontrado" }

if (Test-Path $prefPath) {
  try {
    $p = Get-Content $prefPath -Raw | ConvertFrom-Json
    if (-not $p.account_info -or @($p.account_info).Count -eq 0) {
      $problems += "account_info ausente no Preferences"
    } else {
      $emails = @($p.account_info | ForEach-Object { $_.email } | Where-Object { $_ })
      Write-Host ("account_info: " + ($emails -join ', '))
    }
  } catch { $problems += "Preferences ilegivel: $($_.Exception.Message)" }
} else { $problems += "Preferences nao encontrado" }

# --- 3) Login Twitch (cookie auth-token) ---
$db = Join-Path $def 'Network\Cookies'
if (Test-Path $db) {
  if ((Get-Process chrome -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0) {
    $notes += "Chrome aberto - leitura do cookie Twitch pulada (lock do DB); conferir na UI"
  } else {
    $py = @'
import sqlite3, sys
try:
    con = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute("SELECT count(*) FROM cookies WHERE host_key LIKE '%twitch%' AND name='auth-token'")
    print("auth-token_count=" + str(cur.fetchone()[0]))
    con.close()
except Exception as e:
    print("auth-token_error=" + str(e))
'@
    $pyOut = $py | python - $db
    if ($pyOut -match 'auth-token_count=([0-9]+)') {
      if ([int]$Matches[1] -gt 0) { Write-Host "Twitch: auth-token PRESENTE (logado)" -ForegroundColor Green }
      else { $problems += "Twitch: auth-token ausente - logar na Twitch antes" }
    } else {
      $notes += "Cookie Twitch indeterminado ($pyOut)"
    }
  }
} else { $notes += "DB de cookies nao encontrado" }

# --- 4) Junction residual (uso acidental) ---
$j = Join-Path $env:LOCALAPPDATA 'Temp\vodrip-cdp-profile'
if (Test-Path $j) { $notes += "Junction residual detectada: $j (apagar com Chrome fechado)" }

Write-Host ""
if ($notes.Count -gt 0) {
  Write-Host "Notas:" -ForegroundColor Yellow
  $notes | ForEach-Object { Write-Host ("  (~) " + $_) -ForegroundColor Yellow }
  Write-Host ""
}
if ($problems.Count -eq 0) {
  Write-Host "PERFIL_OK - Chrome do usuario no perfil normal, seguro acessar." -ForegroundColor Green
  exit 0
} else {
  Write-Host "PERFIL_ANORMAL - NAO acessar o Chrome:" -ForegroundColor Red
  $problems | ForEach-Object { Write-Host ("  [!] " + $_) -ForegroundColor Red }
  exit 1
}
