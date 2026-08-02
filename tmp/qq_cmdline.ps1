$qq = Get-CimInstance Win32_Process -Filter "Name='QQ.exe'"
foreach ($q in $qq) {
    $gp = Get-Process -Id $q.ProcessId -ErrorAction SilentlyContinue
    "pid={0} parent={1} title=[{2}]" -f $q.ProcessId, $q.ParentProcessId, $gp.MainWindowTitle
    "    cmd: $($q.CommandLine)"
}
