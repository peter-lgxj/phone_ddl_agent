# 启动本地 Qwen3-VL-8B（llama.cpp llama-server，OpenAI 兼容）
#
# 【可选路径】6 GB 显存跑 8B Q4 + mmproj 偏紧（视觉编码器只能留 CPU，带图约 75-80 秒/次），
# 默认执行模型仍是云端 deepseek-flash。权重、mmproj 与运行时需自行下载或准备。
#
# 6 GB 显存（RTX 3060 Laptop）的取舍：
#   - 文本层全部卸载到 GPU（-ngl 36），实测占用约 5.3 GB
#   - 视觉编码器留在 CPU（--no-mmproj-offload），否则会和文本层抢显存
#   - KV cache 量化到 q8_0，上下文 8192
# 代价：带图请求约 75-80 秒/次（视觉编码在 CPU）；纯文本请求约 10 秒级。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\start_local_vlm.ps1 -BaseDir D:\qwen3vl-local
#   powershell -ExecutionPolicy Bypass -File .\start_local_vlm.ps1 -NGpuLayers 28
# 未指定 -BaseDir 时读取 LOCAL_VLM_HOME，再退回 %LOCALAPPDATA%\qwen3vl-local。
param(
    [int]$Port = 8080,
    [int]$NGpuLayers = 36,
    [int]$Ctx = 8192,
    [int]$Threads = 12,
    [string]$BaseDir = "",
    [string]$ModelDir = "",
    [string]$LlamaDir = ""
)

$ErrorActionPreference = "Stop"
if (-not $BaseDir) {
    if ($env:LOCAL_VLM_HOME) { $BaseDir = $env:LOCAL_VLM_HOME }
    else { $BaseDir = Join-Path $env:LOCALAPPDATA "qwen3vl-local" }
}
if (-not $ModelDir) { $ModelDir = Join-Path $BaseDir "models" }
if (-not $LlamaDir) { $LlamaDir = Join-Path $BaseDir "runtime\llama-extract" }

# 权重与缓存跟随 -BaseDir，避免写死某台机器盘符
$env:HF_HOME = Join-Path $BaseDir "hf-cache"
$env:HF_HUB_CACHE = Join-Path $env:HF_HOME "hub"

$Model  = Join-Path $ModelDir "Qwen3VL-8B-Instruct-Q4_K_M.gguf"
$Mmproj = Join-Path $ModelDir "mmproj-Qwen3VL-8B-Instruct-F16.gguf"
$Server = Join-Path $LlamaDir "llama-server.exe"

foreach ($p in @($Model, $Mmproj, $Server)) {
    if (-not (Test-Path $p)) { throw "缺少文件：$p" }
}

Write-Host "模型：$Model"
Write-Host "视觉投影：$Mmproj"
Write-Host "服务：http://127.0.0.1:$Port/v1（OpenAI 兼容，模型名 qwen3-vl-8b-instruct）"
Write-Host "GPU 层数：$NGpuLayers，上下文：$Ctx，KV：q8_0，视觉编码器：CPU"

& $Server `
    -m $Model `
    --mmproj $Mmproj `
    --no-mmproj-offload `
    --alias qwen3-vl-8b-instruct `
    --host 127.0.0.1 `
    --port $Port `
    -c $Ctx `
    -ngl $NGpuLayers `
    -t $Threads `
    --cache-type-k q8_0 `
    --cache-type-v q8_0 `
    --image-min-tokens 1024 `
    --jinja
