Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Select-Object ProcessId, @{N='RAM_MB'; E={[math]::Round($_.WorkingSetSize / 1MB)}}, CommandLine |
  Format-List | Out-String -Width 250
