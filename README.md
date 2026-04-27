# LoRA Image Prep — LoRA 图像预处理

上游仓库：**<https://github.com/AgnesClaudel/lora-asset-prep>**（仓库名 `lora-asset-prep`）。

基于 PySide6 的素材目录批处理：等比缩放、黑/白/透明补边、竖横分流尺寸、比例预设、排除目录、自动/手动模式；配置写入 `presets.json`。

## 环境

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## 运行

```powershell
python app.py
```

## 打包 exe（可选）

```powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name lora-asset-prep app.py
```

产物在 `dist\lora-asset-prep.exe`。

## 关联远程仓库（首次）

```powershell
git init
git remote add origin https://github.com/AgnesClaudel/lora-asset-prep.git
```
