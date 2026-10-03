# Запускает три сервера llama.cpp в фоне: языковая модель (8080),
# векторы bge-m3 (8081), переранжировщик bge-reranker-v2-m3 (8082).
# Логи пишутся в папку logs. Остановка: scripts\stop_servers.bat
#   scripts\start_servers.bat          — с видеокартой
#   scripts\start_servers.bat -Cpu     — только процессор
param([switch]$Cpu)

$root = (Resolve-Path "$PSScriptRoot\..").Path
$bin = Join-Path $root "tools\llama.cpp\llama-server.exe"
$models = Join-Path $root "models"
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logs | Out-Null

if (-not (Test-Path $bin)) {
    Write-Host "Не найден $bin. Сначала выполните: python scripts\setup.py" -ForegroundColor Red
    exit 1
}

$ngl = if ($Cpu) { "0" } else { "999" }
$servers = @(
    @{ Name = "llm"; Port = 8080; Args = @("-m", "$models\GigaChat3.1-10B-A1.8B-q4_K_M.gguf", "--jinja",
        "-ngl", $ngl, "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "-c", "16384", "-np", "1") },
    @{ Name = "embed"; Port = 8081; Args = @("-m", "$models\bge-m3-Q8_0.gguf", "--embedding", "--pooling", "cls",
        "-ngl", $ngl, "-c", "8192", "-b", "8192", "-ub", "8192") },
    @{ Name = "rerank"; Port = 8082; Args = @("-m", "$models\bge-reranker-v2-m3-Q8_0.gguf", "--reranking",
        "-ngl", $ngl, "-c", "8192", "-b", "8192", "-ub", "8192") }
)

function Test-Health($port) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/health" -TimeoutSec 2
        return $r.StatusCode -eq 200
    } catch { return $false }
}

foreach ($s in $servers) {
    if (Test-Health $s.Port) { Write-Host "$($s.Name): уже работает на порту $($s.Port)"; continue }
    $argList = ($s.Args + @("--host", "127.0.0.1", "--port", "$($s.Port)")) |
        ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
    Start-Process -FilePath $bin -ArgumentList $argList -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "$($s.Name).out.log") `
        -RedirectStandardError (Join-Path $logs "$($s.Name).log")
    Write-Host "$($s.Name): запускается на порту $($s.Port)…"
}

$deadline = (Get-Date).AddMinutes(3)
foreach ($s in $servers) {
    while (-not (Test-Health $s.Port)) {
        if ((Get-Date) -gt $deadline) {
            Write-Host "$($s.Name): не запустился, смотрите logs\$($s.Name).log" -ForegroundColor Red
            exit 1
        }
        Start-Sleep -Seconds 1
    }
    Write-Host "$($s.Name): готов" -ForegroundColor Green
}
Write-Host "Все модели запущены. Чат: scripts\run_chat.bat"
