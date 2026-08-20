"""PyInstaller recipe for the single-file Windows studio server."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = Path(SPECPATH)

datas = [
    (str(project_root / "webapp" / "static"), "webapp/static"),
    (str(project_root / "assets" / "branding"), "assets/branding"),
    (str(project_root / "dorama" / "chatterbox_worker.py"), "dorama"),
]
for package in ("certifi", "edge_tts", "imageio_ffmpeg", "yt_dlp"):
    datas.extend(collect_data_files(package))

hidden_imports = {
    "cli",
    "dorama.discover",
    "dorama.licensed_sources",
    "dorama.literal_translation",
    "dorama.pipeline",
    "dorama.source_pipeline",
    "publisher.telegram",
    "publisher.youtube",
    "scheduler",
    "telegram_bot.bot",
    *collect_submodules("yt_dlp"),
}

analysis = Analysis(
    [str(project_root / "server_launcher.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(hidden_imports),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["chatterbox", "pytest", "ruff", "mypy", "pip_audit", "torch"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="Dorama Studio Server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(project_root / "assets" / "branding" / "avatar-dorama-za-5.png"),
)
