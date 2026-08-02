$procs = Get-CimInstance Win32_Process -Filter "Name='NapCatWinBootMain.exe' or Name='QQ.exe' or Name='cmd.exe'" |
  Where-Object { $_.Name -ne 'cmd.exe' -or $_.CommandLine -match 'NapCat' }
$procs | Select-Object ProcessId, ParentProcessId, Name, CreationDate |
  Sort-Object Name, CreationDate |
  Format-Table -AutoSize | Out-String -Width 200
