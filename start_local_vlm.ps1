# Start the local Qwen3-VL-8B llama.cpp server on this machine.
# Usage: powershell -ExecutionPolicy Bypass -File .\start_local_vlm.ps1
param(
    [int]$Port = 18080,
    [string]$NGpuLayers = 'auto',
    [int]$Ctx = 8192,
    [int]$Threads = 12,
    [string]$ModelDir = "",
    [string]$LlamaDir = ""
)

$ErrorActionPreference = "Stop"
$BaseDir = "D:\tools\LLM"
if (-not $ModelDir) { $ModelDir = Join-Path $BaseDir "Qwen3-VL-8B-Instruct-GGUF" }
if (-not $LlamaDir) { $LlamaDir = Join-Path $BaseDir "llama.cpp" }

# Keep Hugging Face caches outside the repository.
$env:HF_HOME = Join-Path $BaseDir "hf-cache"
$env:HF_HUB_CACHE = Join-Path $env:HF_HOME "hub"

$Model  = Join-Path $ModelDir "Qwen3VL-8B-Instruct-Q4_K_M.gguf"
$Mmproj = Join-Path $ModelDir "mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"
$Server = Join-Path $LlamaDir "llama-server.exe"

foreach ($p in @($Model, $Mmproj, $Server)) {
    if (-not (Test-Path $p)) { throw "Missing file: $p" }
}

Write-Host "Model: $Model"
Write-Host "Projector: $Mmproj"
Write-Host "Service: http://127.0.0.1:$Port/v1 (model qwen3-vl-8b-local)"
Write-Host "GPU layers: $NGpuLayers; context: $Ctx; KV: q8_0"

& $Server `
    -m $Model `
    --mmproj $Mmproj `
    --alias qwen3-vl-8b-local `
    --host 127.0.0.1 `
    --port $Port `
    -c $Ctx `
    -np 1 `
    -ngl $NGpuLayers `
    -t $Threads `
    --cache-type-k q8_0 `
    --cache-type-v q8_0 `
    --image-min-tokens 1024 `
    --jinja
