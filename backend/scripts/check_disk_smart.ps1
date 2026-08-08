$ErrorActionPreference = 'SilentlyContinue'
'--- Physical disks (health) ---'
Get-PhysicalDisk | Select-Object DeviceId, FriendlyName, MediaType, HealthStatus, OperationalStatus, @{n='SizeGB';e={[math]::Round($_.Size/1GB)}} | Format-Table -AutoSize
'--- SMART-ish (reliability counters) ---'
Get-PhysicalDisk | Get-StorageReliabilityCounter | Select-Object DeviceId, Temperature, Wear, ReadErrorsTotal, WriteErrorsTotal, ReadErrorsUncorrected, WriteErrorsUncorrected | Format-Table -AutoSize
'--- Which physical disk is C:? ---'
Get-Partition -DriveLetter C | Get-Disk | Select-Object Number, FriendlyName, @{n='SizeGB';e={[math]::Round($_.Size/1GB)}} | Format-Table -AutoSize
