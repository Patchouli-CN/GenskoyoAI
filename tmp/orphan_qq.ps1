$all = Get-CimInstance Win32_Process
$aliveIds = @{}
foreach ($p in $all) { $aliveIds[[int]$p.ProcessId] = $true }
$qq = @($all | Where-Object Name -eq 'QQ.exe')
foreach ($q in $qq) {
    $parentAlive = $aliveIds.ContainsKey([int]$q.ParentProcessId)
    "{0}  parent={1}  parentAlive={2}  created={3}" -f $q.ProcessId, $q.ParentProcessId, $parentAlive, $q.CreationDate
}
"--- orphan QQ (bot candidates) ---"
foreach ($q in $qq) {
    if (-not $aliveIds.ContainsKey([int]$q.ParentProcessId)) { $q.ProcessId }
}
