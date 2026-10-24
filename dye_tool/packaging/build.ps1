# dye_tool 打包脚本（Windows / PowerShell）
#
# 用法（项目根目录下）：
#     pwsh -File packaging\build.ps1
#
# 产物：dist\dye_tool\dye_tool.exe（one-folder，免安装，双击即运行）
# 说明：使用项目内 .venv；若不存在会先用 Python 3.11 创建并安装依赖。

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Venv = Join-Path $Root ".venv"
$Py   = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Host "[1/4] 创建虚拟环境 .venv ..." -ForegroundColor Cyan
    $base = (Get-Command python).Source
    & $base -m venv $Venv
    & $Py -m pip install --upgrade pip -q
    Write-Host "[2/4] 安装依赖 ..." -ForegroundColor Cyan
    & $Py -m pip install -r (Join-Path $Root "requirements.txt") pyinstaller
} else {
    Write-Host "[1/4] 复用已有虚拟环境 .venv" -ForegroundColor Cyan
    Write-Host "[2/4] 检查依赖 ..." -ForegroundColor Cyan
    & $Py -m pip install -q -r (Join-Path $Root "requirements.txt") pyinstaller
}

Write-Host "[3/4] 打包（V2.8：两道具门已从打包链删除；验证由 check.py full 承担）..." -ForegroundColor Cyan

# ── 先清旧产物：必须带重试 ──────────────────────────────────────────────
# 实测（V1.3）：直接 `Remove-Item dist -Recurse` 有**大概率**撞上
# `PermissionError [WinError 5]`——杀软实时扫描 / 资源管理器缩略图 / 搜索索引器
# 会短暂持有刚生成的文件句柄（最常被抓住的是 samples 下的 .TXT）。
# 它是**瞬时**的：等一会儿再删就成功。所以这里带退避重试；
# 一直删不掉就**明确报错**而不是让 PyInstaller 在后面报个难懂的错。
$Dist = Join-Path $Root "dist"
function Remove-DistWithRetry {
    param([string]$Path, [int]$Tries = 6)
    for ($i = 1; $i -le $Tries; $i++) {
        if (-not (Test-Path $Path)) { return $true }
        try {
            Remove-Item $Path -Recurse -Force -ErrorAction Stop
            if (-not (Test-Path $Path)) { return $true }
        } catch {
            $msg = "      第 {0}/{1} 次清理失败（多为杀软/索引器瞬时占用），{2} ms 后重试…" -f `
                   $i, $Tries, (400 * $i)
            Write-Host $msg -ForegroundColor DarkYellow
        }
        Start-Sleep -Milliseconds (400 * $i)
    }
    return -not (Test-Path $Path)
}

# V2.9.3 M5：旧版 zip 默认保留（设计文档 §3.6）——先备份 dist 根 zip，打包后还原
$ZipBackup = Join-Path $Root "dist_zips_backup"
if (Test-Path $Dist) {
    $oldZips = Get-ChildItem $Dist -Filter "*.zip" -File -ErrorAction SilentlyContinue
    if ($oldZips) {
        New-Item -ItemType Directory -Force -Path $ZipBackup | Out-Null
        $oldZips | Copy-Item -Destination $ZipBackup -Force
        Write-Host "      已备份 $($oldZips.Count) 个旧 zip（打包后还原）" -ForegroundColor DarkGray
    }
}

if (-not (Remove-DistWithRetry -Path $Dist)) {
    throw ("无法清空 $Dist —— 有进程仍持有其中文件。" +
           "请关闭已打开的 dye_tool.exe、资源管理器预览窗格，或把 dist 加入杀软排除目录后重试。")
}

Write-Host "[4/4] 同步 exe 版本资源（L17）..." -ForegroundColor Cyan
# L17：把 core/__init__.py 的 VERSION 写进 packaging\version_info.txt，
# 这样 exe 的"属性 → 详细信息"里显示的就是真版本，而版本号仍只有一处真相。
$VerSrc = Join-Path $Root "core\__init__.py"
$VerTpl = Join-Path $Root "packaging\version_info.txt"
$m = [regex]::Match((Get-Content $VerSrc -Raw -Encoding UTF8), 'VERSION\s*=\s*"([0-9]+)\.([0-9]+)\.([0-9]+)"')
if (-not $m.Success) { throw "无法从 $VerSrc 解析 VERSION（期望形如 VERSION = `"1.3.0`"）" }
$vMaj = [int]$m.Groups[1].Value; $vMin = [int]$m.Groups[2].Value; $vPat = [int]$m.Groups[3].Value
$vFull = "$vMaj.$vMin.$vPat"
$vQuad = "$vMaj.$vMin.$vPat.0"
$txt = [System.IO.File]::ReadAllText($VerTpl, [System.Text.Encoding]::UTF8)
# 只改版本字段（filevers/prodvers 是元组，FileVersion 是四段，ProductVersion 是三段）
$txt = [regex]::Replace($txt, 'filevers=\([0-9, ]+\)', "filevers=($vMaj, $vMin, $vPat, 0)")
$txt = [regex]::Replace($txt, 'prodvers=\([0-9, ]+\)', "prodvers=($vMaj, $vMin, $vPat, 0)")
$txt = [regex]::Replace($txt, 'StringStruct\("FileVersion", "[^"]*"\)', "StringStruct(`"FileVersion`", `"$vQuad`")")
$txt = [regex]::Replace($txt, 'StringStruct\("ProductVersion", "[^"]*"\)', "StringStruct(`"ProductVersion`", `"$vFull`")")
# 必须是 UTF-8 **带 BOM**：PyInstaller 用 utf-8-sig 读这个模板，无 BOM 时中文会乱码
[System.IO.File]::WriteAllText($VerTpl, $txt, (New-Object System.Text.UTF8Encoding($true)))
Write-Host "      version_info.txt -> $vFull" -ForegroundColor DarkGray

Write-Host "[4/4] PyInstaller 打包 ..." -ForegroundColor Cyan
& $Py -m PyInstaller (Join-Path $Root "packaging\dye_tool.spec") --noconfirm

$Exe = Join-Path $Root "dist\dye_tool\dye_tool.exe"
if (Test-Path $Exe) {
    # 把用户可见的文件放到 exe 同级（PyInstaller one-folder 默认塞进 _internal）
    $OutDir = Join-Path $Root "dist\dye_tool"
    Copy-Item (Join-Path $Root "config.yaml") $OutDir -Force
    Copy-Item (Join-Path $Root "README.md")   $OutDir -Force
    # V2.9 FR-59 甲：随包 QUICKSTART（三步上手/命名建议/剔除原因/CLI 示例）
    Copy-Item (Join-Path $Root "packaging\QUICKSTART.txt") $OutDir -Force
    # 公开仓库不携带实验样例数据；若本地存在 samples/ 才随包分发。
    $samples = Join-Path $Root "samples"
    if (Test-Path $samples) {
        Copy-Item $samples $OutDir -Recurse -Force
    }

    $size = [math]::Round((Get-ChildItem $OutDir -Recurse -File |
             Measure-Object -Property Length -Sum).Sum / 1MB, 1)
    # 还原旧版 zip（默认不删；保留与旧包并存）
    if (Test-Path $ZipBackup) {
        Get-ChildItem $ZipBackup -Filter "*.zip" -File | Copy-Item -Destination $Dist -Force
        Remove-Item $ZipBackup -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "`n[完成] $Exe  (总大小约 $size MB)" -ForegroundColor Green
    Write-Host "       双击即可运行；config.yaml 与 QUICKSTART.txt 已随包。" -ForegroundColor Green
} else {
    throw "打包失败：未找到 $Exe"
}
