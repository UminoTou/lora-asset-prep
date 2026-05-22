import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image
from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
PRESETS_FILE = "presets.json"


def _sorted_exclude_pick_paths(paths: list[str]) -> list[str]:
    """多选对话框返回顺序不稳定；按路径层级与不区分大小写的文件名排序，与资源管理器中「名称」升序观感一致。"""
    uniq: list[str] = []
    seen: set[str] = set()
    for p in paths:
        norm = os.path.normpath(p)
        if norm not in seen:
            seen.add(norm)
            uniq.append(norm)
    return sorted(uniq, key=lambda fp: tuple(part.lower() for part in Path(fp).parts))


APP_NAME_EN = "LoRA Asset Prep"
APP_NAME_ZH = "LoRA 素材预处理"
WINDOW_TITLE = f"{APP_NAME_EN} — {APP_NAME_ZH}"
# 仓库 slug（kebab-case），与 https://github.com/AgnesClaudel/lora-asset-prep 对应
APP_REPO_SLUG = "lora-asset-prep"


@dataclass
class AppConfig:
    source_paths: list[str] = field(default_factory=list)
    target_width: int = 768
    target_height: int = 1344
    split_long_wide: bool = False
    long_target_width: int = 768
    long_target_height: int = 1344
    wide_target_width: int = 1344
    wide_target_height: int = 768
    use_exclude_dirs: bool = False
    excluded_paths: list[str] = field(default_factory=list)
    recursive: bool = True
    resize_images: bool = True
    padding_color: str = "black"  # "black" | "white" | "transparent"


class Worker(QObject):
    progress = Signal(int, int)
    log = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, cfg: AppConfig):
        super().__init__()
        self.cfg = cfg
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            result = process_dataset(self.cfg, self._emit_log, self._emit_progress, self._is_cancelled)
            self.finished.emit(result)
        except Exception as exc:  # pragma: no cover
            self.failed.emit(str(exc))

    def _emit_log(self, msg: str) -> None:
        self.log.emit(msg)

    def _emit_progress(self, current: int, total: int) -> None:
        self.progress.emit(current, total)

    def _is_cancelled(self) -> bool:
        return self._cancelled


def path_matches_exclusion(path: Path, exclusions: list[Path]) -> bool:
    """排除项可为目录（整棵跳过）或单个图片路径（仅该文件）。"""
    try:
        pr = path.resolve()
    except OSError:
        return False
    for ex in exclusions:
        try:
            er = ex.resolve()
        except OSError:
            continue
        if not er.exists():
            continue
        if er.is_file():
            if pr == er:
                return True
        else:
            if pr == er:
                return True
            try:
                pr.relative_to(er)
                return True
            except ValueError:
                pass
    return False


def iter_image_files(
    source_root: Path,
    recursive: bool,
    exclusions: list[Path],
):
    if recursive:
        for dirpath, dirnames, filenames in os.walk(source_root, topdown=True):
            current = Path(dirpath)
            if exclusions and path_matches_exclusion(current, exclusions):
                dirnames[:] = []
                continue
            for name in filenames:
                p = current / name
                if p.suffix.lower() not in IMAGE_EXTS:
                    continue
                if exclusions and path_matches_exclusion(p, exclusions):
                    continue
                yield p
    else:
        for p in source_root.iterdir():
            if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
                continue
            if exclusions and path_matches_exclusion(p, exclusions):
                continue
            yield p


def _normalized_unique_paths(paths: Iterable[str]) -> list[str]:
    """仅保留磁盘上存在的路径，绝对规范化、去重并保持首次出现顺序。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        s = (raw or "").strip()
        if not s:
            continue
        ap = os.path.normpath(os.path.abspath(os.path.expanduser(s)))
        if not os.path.exists(ap):
            continue
        try:
            key = str(Path(ap).resolve()).lower()
        except OSError:
            key = ap.lower()
        if key not in seen:
            seen.add(key)
            out.append(ap)
    return out


def collect_process_image_paths(cfg: AppConfig) -> tuple[list[Path], list[str]]:
    """枚举待处理图片（可多根目录或多文件）；第二项为人类可读错误信息列表。"""
    exclusions: list[Path] = []
    if cfg.use_exclude_dirs:
        for s in cfg.excluded_paths:
            t = s.strip()
            if not t:
                continue
            p = Path(t)
            if p.exists():
                exclusions.append(p.resolve())

    roots_raw = [str(x).strip() for x in cfg.source_paths if str(x).strip()]
    if not roots_raw:
        return [], ["请先添加至少一个素材文件夹或图片文件。"]

    missing = [rs for rs in roots_raw if not Path(rs).exists()]
    if missing:
        msgs = [f"{len(missing)} 个素材路径不存在："] + missing[:12]
        if len(missing) > 12:
            msgs.append("…")
        return [], msgs

    seen_k: set[str] = set()
    basket: list[Path] = []
    for rs in roots_raw:
        root = Path(rs)
        try:
            r = root.resolve()
        except OSError:
            continue
        if r.is_file():
            if r.suffix.lower() not in IMAGE_EXTS:
                continue
            if exclusions and path_matches_exclusion(r, exclusions):
                continue
            k = str(r).lower()
            if k not in seen_k:
                seen_k.add(k)
                basket.append(r)
        elif r.is_dir():
            for p in iter_image_files(r, cfg.recursive, exclusions):
                try:
                    kr = str(p.resolve()).lower()
                except OSError:
                    kr = str(p).lower()
                if kr not in seen_k:
                    seen_k.add(kr)
                    basket.append(p)

    basket.sort(key=lambda fp: tuple(x.lower() for x in fp.parts))
    return basket, []


def pick_target_size(img_w: int, img_h: int, cfg: AppConfig) -> tuple[int, int]:
    if not cfg.split_long_wide:
        return cfg.target_width, cfg.target_height
    if img_w >= img_h:
        return cfg.wide_target_width, cfg.wide_target_height
    return cfg.long_target_width, cfg.long_target_height


def resize_with_padding(
    image: Image.Image,
    target_w: int,
    target_h: int,
    pad: str,
) -> Image.Image:
    w, h = image.size
    scale = min(target_w / w, target_h / h)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)

    if (new_w, new_h) == (target_w, target_h):
        return resized

    p = pad.lower()
    if p == "transparent":
        src = resized if resized.mode == "RGBA" else resized.convert("RGBA")
        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        x = (target_w - new_w) // 2
        y = (target_h - new_h) // 2
        canvas.paste(src, (x, y))
        return canvas

    is_white = p == "white"
    if resized.mode == "RGBA":
        fill = (255, 255, 255, 255) if is_white else (0, 0, 0, 255)
        canvas = Image.new("RGBA", (target_w, target_h), fill)
    elif resized.mode == "L":
        fill = 255 if is_white else 0
        canvas = Image.new("L", (target_w, target_h), fill)
    else:
        if resized.mode != "RGB":
            resized = resized.convert("RGB")
        fill = (255, 255, 255) if is_white else (0, 0, 0)
        canvas = Image.new("RGB", (target_w, target_h), fill)
    x = (target_w - new_w) // 2
    y = (target_h - new_h) // 2
    canvas.paste(resized, (x, y))
    return canvas


def _config_from_dict(raw: dict) -> AppConfig:
    defaults = asdict(AppConfig())
    merged = {**defaults, **raw}
    legacy_dir = merged.get("source_dir", "") if isinstance(merged.get("source_dir"), str) else ""
    sp = merged.get("source_paths")
    if isinstance(sp, list):
        merged["source_paths"] = [str(x).strip() for x in sp if str(x).strip()]
    else:
        merged["source_paths"] = []
    if not merged["source_paths"] and legacy_dir.strip():
        merged["source_paths"] = [legacy_dir.strip()]
    for k in ("replace_from", "replace_to", "replace_tags", "jpeg_quality", "excluded_dirs", "source_dir"):
        merged.pop(k, None)
    fields = set(defaults.keys())
    cfg_dict = {k: merged[k] for k in fields if k in merged}
    if not isinstance(cfg_dict.get("excluded_paths"), list):
        cfg_dict["excluded_paths"] = []
    else:
        cfg_dict["excluded_paths"] = [str(x) for x in cfg_dict["excluded_paths"]]
    if cfg_dict.get("padding_color") not in ("black", "white", "transparent"):
        cfg_dict["padding_color"] = "black"
    return AppConfig(**cfg_dict)


def process_dataset(
    cfg: AppConfig,
    log: Callable[[str], None],
    progress: Callable[[int, int], None],
    is_cancelled: Callable[[], bool],
) -> dict:
    images, errs = collect_process_image_paths(cfg)
    if errs:
        raise RuntimeError("\n".join(errs))

    total = len(images)
    progress(0, total)
    if total == 0:
        log("没有找到可处理的图片。")
        return {
            "total": 0,
            "resized": 0,
            "skipped_size_ok": 0,
            "errors": 0,
            "cancelled": False,
        }

    stats = {
        "total": total,
        "resized": 0,
        "skipped_size_ok": 0,
        "errors": 0,
        "cancelled": False,
    }

    pad = cfg.padding_color if cfg.padding_color in ("black", "white", "transparent") else "black"
    jpg_warned = [False]

    for idx, image_path in enumerate(images, start=1):
        if is_cancelled():
            stats["cancelled"] = True
            log("用户取消任务。")
            break

        try:
            if cfg.resize_images:
                resized: Image.Image | None = None
                original_fmt: str | None = None
                with Image.open(image_path) as img:
                    original_fmt = img.format
                    tw, th = pick_target_size(*img.size, cfg)
                    if img.size == (tw, th):
                        resized = None
                    else:
                        resized = resize_with_padding(img.copy(), tw, th, pad)
                if resized is None:
                    stats["skipped_size_ok"] += 1
                else:
                    save_kwargs: dict = {}
                    resized = _prepare_save_image(
                        resized,
                        image_path,
                        original_fmt,
                        pad,
                        log,
                        jpg_warned,
                    )
                    if original_fmt:
                        resized.save(image_path, format=original_fmt, **save_kwargs)
                    else:
                        resized.save(image_path, **save_kwargs)
                    stats["resized"] += 1
        except Exception as exc:
            stats["errors"] += 1
            log(f"[失败] {image_path.name}: {exc}")

        progress(idx, total)
        if idx % 10 == 0 or idx == total:
            log(f"处理进度: {idx}/{total}")

    return stats


def _prepare_save_image(
    out: Image.Image,
    image_path: Path,
    original_fmt: str | None,
    pad: str,
    log: Callable[[str], None],
    jpg_transparent_warned: list[bool],
) -> Image.Image:
    if image_path.suffix.lower() in {".jpg", ".jpeg"}:
        if out.mode == "RGBA" and pad == "transparent":
            if not jpg_transparent_warned[0]:
                log("JPEG 不支持透明通道，透明补边已用白底合并保存。")
                jpg_transparent_warned[0] = True
            bg = Image.new("RGB", out.size, (255, 255, 255))
            bg.paste(out, mask=out.split()[3])
            out = bg
        elif out.mode != "RGB":
            out = out.convert("RGB")
    return out


def presets_path() -> Path:
    return Path(__file__).resolve().parent / PRESETS_FILE


def load_presets_store() -> dict:
    p = presets_path()
    out = {
        "last_preset": "",
        "presets": {},
        "size_presets": {},
        "last_size_preset": "",
    }
    if not p.exists():
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return out
    if not isinstance(data, dict):
        return out
    presets = data.get("presets")
    out["presets"] = presets if isinstance(presets, dict) else {}
    last = data.get("last_preset", "")
    out["last_preset"] = last if isinstance(last, str) else ""
    size_presets = data.get("size_presets")
    out["size_presets"] = size_presets if isinstance(size_presets, dict) else {}
    ls = data.get("last_size_preset", "")
    out["last_size_preset"] = ls if isinstance(ls, str) else ""
    return out


def compact_wh_row(width_spin: QSpinBox, height_spin: QSpinBox) -> QWidget:
    """「宽」紧挨宽度框、「高」紧挨高度框，两组之间留小间距。"""
    w = QWidget()
    outer = QHBoxLayout(w)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(14)
    pair_spacing = 4
    for text, spin in (("宽", width_spin), ("高", height_spin)):
        inner = QHBoxLayout()
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(pair_spacing)
        inner.addWidget(QLabel(text))
        inner.addWidget(spin)
        outer.addLayout(inner)
    outer.addStretch(1)
    return w


def save_full_store(store: dict) -> None:
    p = presets_path()
    payload = {
        "last_preset": store.get("last_preset", ""),
        "presets": store.get("presets", {}) if isinstance(store.get("presets"), dict) else {},
        "size_presets": store.get("size_presets", {})
        if isinstance(store.get("size_presets"), dict)
        else {},
        "last_size_preset": store.get("last_size_preset", "")
        if isinstance(store.get("last_size_preset"), str)
        else "",
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class SourcePathsList(QListWidget):
    pathsDropped = Signal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(115)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDropIndicatorShown(True)
        self.setToolTip("将文件夹或多位图片拖入此处，或点击下方按钮批量添加。\n可多选条目后点「移除所选」。")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            raw = [u.toLocalFile().strip() for u in event.mimeData().urls() if u.isLocalFile()]
            paths = [p for p in raw if p]
            if paths:
                self.pathsDropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(820, 720)

        self.thread: QThread | None = None
        self.worker: Worker | None = None
        # 上次在对话框中打开的目录仍存在时，用作下次「选择文件夹/选择图片」的起始路径
        self._dialog_start_dir: str | None = None

        self.source_list = SourcePathsList(self)
        self.source_list.pathsDropped.connect(self._on_sources_dropped)
        self.add_sources_folder_btn = QPushButton("添加文件夹…")
        self.add_sources_folder_btn.clicked.connect(self.select_sources_folder)
        self.add_sources_files_btn = QPushButton("添加图片…")
        self.add_sources_files_btn.clicked.connect(self.select_sources_files)
        self.remove_sources_btn = QPushButton("移除所选")
        self.remove_sources_btn.clicked.connect(self._remove_selected_sources)
        self.clear_sources_btn = QPushButton("清空素材")
        self.clear_sources_btn.clicked.connect(self._clear_sources_list)

        self.width_input = QSpinBox()
        self.width_input.setRange(64, 8192)
        self.width_input.setMaximumWidth(100)
        self.height_input = QSpinBox()
        self.height_input.setRange(64, 8192)
        self.height_input.setMaximumWidth(100)

        self.size_mode_unified = QRadioButton("统一尺寸")
        self.size_mode_split = QRadioButton("竖横分流")
        self.size_mode_unified.setChecked(True)
        self.size_mode_group = QButtonGroup(self)
        self.size_mode_group.addButton(self.size_mode_unified)
        self.size_mode_group.addButton(self.size_mode_split)
        self.long_w_input = QSpinBox()
        self.long_w_input.setRange(64, 8192)
        self.long_w_input.setMaximumWidth(100)
        self.long_h_input = QSpinBox()
        self.long_h_input.setRange(64, 8192)
        self.long_h_input.setMaximumWidth(100)
        self.wide_w_input = QSpinBox()
        self.wide_w_input.setRange(64, 8192)
        self.wide_w_input.setMaximumWidth(100)
        self.wide_h_input = QSpinBox()
        self.wide_h_input.setRange(64, 8192)
        self.wide_h_input.setMaximumWidth(100)

        self.single_size_widget = QWidget()
        self.split_size_widget = QWidget()

        self.size_preset_combo = QComboBox()
        self.size_preset_combo.setMinimumWidth(160)
        self.size_preset_combo.activated.connect(self.apply_selected_size_preset)
        self.apply_size_preset_btn = QPushButton("应用所选比例")
        self.apply_size_preset_btn.clicked.connect(self.apply_selected_size_preset)
        self.save_size_preset_btn = QPushButton("保存当前比例")
        self.save_size_preset_btn.clicked.connect(self.save_size_preset_as)
        self.delete_size_preset_btn = QPushButton("删除所选比例")
        self.delete_size_preset_btn.clicked.connect(self.delete_selected_size_preset)

        self.exclude_enable = QCheckBox("排除所选文件夹/图片")
        self.exclude_rows_host = QWidget()
        self._exclude_layout = QVBoxLayout(self.exclude_rows_host)
        self._exclude_layout.setContentsMargins(0, 0, 0, 0)
        self._exclude_row_edits: list[QLineEdit] = []
        self.add_exclude_row_btn = QPushButton("+ 添加排除路径行")
        self.add_exclude_row_btn.clicked.connect(lambda: self._add_exclude_row(""))

        self.recursive_checkbox = QCheckBox("递归处理子目录")

        self.resize_checkbox = QCheckBox("等比缩放并补边")
        self.pad_black = QRadioButton("黑边")
        self.pad_white = QRadioButton("白边")
        self.pad_transparent = QRadioButton("透明边")
        self.pad_black.setChecked(True)
        self.pad_group = QButtonGroup(self)
        self.pad_group.addButton(self.pad_black)
        self.pad_group.addButton(self.pad_white)
        self.pad_group.addButton(self.pad_transparent)

        self.run_btn = QPushButton("开始处理")
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)

        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(200)
        self.load_preset_btn = QPushButton("加载所选预设")
        self.load_preset_btn.clicked.connect(self.load_selected_preset)
        self.save_preset_btn = QPushButton("保存为预设…")
        self.save_preset_btn.clicked.connect(self.save_preset_as)
        self.update_preset_btn = QPushButton("更新当前预设")
        self.update_preset_btn.clicked.connect(self.update_current_preset)
        self.rename_preset_btn = QPushButton("重命名预设…")
        self.rename_preset_btn.clicked.connect(self.rename_preset)
        self.delete_preset_btn = QPushButton("删除所选预设")
        self.delete_preset_btn.clicked.connect(self.delete_selected_preset)

        self.progress_bar = QProgressBar()
        self.status_label = QLabel("等待执行")
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)

        self.run_btn.clicked.connect(self.start_task)
        self.cancel_btn.clicked.connect(self.cancel_task)
        self.exclude_enable.toggled.connect(self._on_exclude_toggled)
        self.size_mode_unified.toggled.connect(self._on_size_mode_toggled)
        self.size_mode_split.toggled.connect(self._on_size_mode_toggled)

        self._build_ui()
        self._update_size_mode_visibility()
        self._clear_exclude_rows()
        self._add_exclude_row("")
        self._refresh_preset_combo()
        self._refresh_size_preset_combo()
        self._load_startup_preset()
        self._on_exclude_toggled(self.exclude_enable.isChecked())

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        layout.addWidget(QLabel("素材（可多选文件夹与图片，支持从资源管理器拖入）："))
        src_btns = QHBoxLayout()
        src_btns.addWidget(self.add_sources_folder_btn)
        src_btns.addWidget(self.add_sources_files_btn)
        src_btns.addWidget(self.remove_sources_btn)
        src_btns.addWidget(self.clear_sources_btn)
        src_btns.addStretch()
        layout.addLayout(src_btns)
        layout.addWidget(self.source_list)

        scan_group = QGroupBox("扫描范围")
        scan_form = QFormLayout(scan_group)
        scan_form.addRow("", self.exclude_enable)
        ex_wrap = QVBoxLayout()
        ex_wrap.addWidget(self.exclude_rows_host)
        ex_wrap.addWidget(self.add_exclude_row_btn)
        ex_w = QWidget()
        ex_w.setLayout(ex_wrap)
        scan_form.addRow("排除路径:", ex_w)
        scan_form.addRow("", self.recursive_checkbox)
        layout.addWidget(scan_group)

        auto_panel = QWidget()
        la = QVBoxLayout(auto_panel)
        base_group = QGroupBox("自动处理")
        base_form = QFormLayout(base_group)
        sz_pr = QHBoxLayout()
        sz_pr.addWidget(self.size_preset_combo)
        sz_pr.addWidget(self.apply_size_preset_btn)
        sz_pr.addWidget(self.save_size_preset_btn)
        sz_pr.addWidget(self.delete_size_preset_btn)
        sz_pr.addStretch()
        sz_pr_w = QWidget()
        sz_pr_w.setLayout(sz_pr)
        base_form.addRow("比例预设:", sz_pr_w)

        single_inner = compact_wh_row(self.width_input, self.height_input)
        single_outer = QVBoxLayout(self.single_size_widget)
        single_outer.setContentsMargins(0, 0, 0, 0)
        single_outer.addWidget(single_inner)
        unified_line = QWidget()
        ul = QHBoxLayout(unified_line)
        ul.setContentsMargins(0, 0, 0, 0)
        ul.addWidget(self.size_mode_unified)
        ul.addWidget(self.single_size_widget)

        split_form = QFormLayout(self.split_size_widget)
        split_form.setContentsMargins(0, 0, 0, 0)
        split_form.addRow("竖图:", compact_wh_row(self.long_w_input, self.long_h_input))
        split_form.addRow("横图:", compact_wh_row(self.wide_w_input, self.wide_h_input))
        split_block = QWidget()
        sb = QVBoxLayout(split_block)
        sb.setContentsMargins(0, 0, 0, 0)
        sr = QHBoxLayout()
        sr.setContentsMargins(0, 0, 0, 0)
        sr.addWidget(self.size_mode_split)
        sr.addStretch()
        sb.addLayout(sr)
        sb.addWidget(self.split_size_widget)

        size_mode_container = QWidget()
        smc = QVBoxLayout(size_mode_container)
        smc.setContentsMargins(0, 0, 0, 0)
        smc.setSpacing(6)
        smc.addWidget(unified_line)
        smc.addWidget(split_block)
        self._size_mode_hint = QLabel("分流：宽<高为竖图，否则为横图。")
        self._size_mode_hint.setWordWrap(True)
        smc.addWidget(self._size_mode_hint)
        base_form.addRow("画布尺寸:", size_mode_container)
        la.addWidget(base_group)

        tag_group = QGroupBox("输出")
        tag_grid = QGridLayout(tag_group)
        tag_grid.addWidget(self.resize_checkbox, 0, 0, 1, 2)
        pad_row = QHBoxLayout()
        pad_row.addWidget(self.pad_black)
        pad_row.addWidget(self.pad_white)
        pad_row.addWidget(self.pad_transparent)
        pad_row.addStretch()
        self.pad_options_widget = QWidget()
        self.pad_options_widget.setLayout(pad_row)
        self.pad_label = QLabel("补边:")
        tag_grid.addWidget(self.pad_label, 1, 0)
        tag_grid.addWidget(self.pad_options_widget, 1, 1)
        la.addWidget(tag_group)

        preset_group = QGroupBox("预设（presets.json）")
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("预设:"))
        preset_row.addWidget(self.preset_combo)
        preset_row.addWidget(self.load_preset_btn)
        preset_row2 = QHBoxLayout()
        preset_row2.addWidget(self.save_preset_btn)
        preset_row2.addWidget(self.update_preset_btn)
        preset_row2.addWidget(self.rename_preset_btn)
        preset_row2.addWidget(self.delete_preset_btn)
        preset_row2.addStretch()
        pg = QVBoxLayout(preset_group)
        pg.addLayout(preset_row)
        pg.addLayout(preset_row2)
        la.addWidget(preset_group)
        la.addStretch(1)
        layout.addWidget(auto_panel, 1)

        action_row = QHBoxLayout()
        action_row.addWidget(self.run_btn)
        action_row.addWidget(self.cancel_btn)
        layout.addLayout(action_row)

        layout.addWidget(self.progress_bar)
        layout.addWidget(self.status_label)
        layout.addWidget(self.log_box)

    def _on_exclude_toggled(self, on: bool) -> None:
        self.exclude_rows_host.setEnabled(on)
        self.add_exclude_row_btn.setEnabled(on)

    def _on_size_mode_toggled(self, _checked: bool = False) -> None:
        self._update_size_mode_visibility()

    def _update_size_mode_visibility(self) -> None:
        unified = self.size_mode_unified.isChecked()
        self.single_size_widget.setVisible(unified)
        self.split_size_widget.setVisible(not unified)
        self._size_mode_hint.setVisible(not unified)

    def _clear_exclude_rows(self) -> None:
        while self._exclude_layout.count():
            item = self._exclude_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._exclude_row_edits.clear()

    def _apply_sorted_exclude_selection(self, trigger_edit: QLineEdit, raw_paths: list[str]) -> None:
        paths = _sorted_exclude_pick_paths(raw_paths)
        if not paths:
            return
        trigger_edit.setText(paths[0])
        pending = paths[1:]
        for edit in self._exclude_row_edits:
            if not pending:
                break
            if edit is trigger_edit:
                continue
            if not edit.text().strip():
                edit.setText(pending.pop(0))
        for p in pending:
            self._add_exclude_row(p)

    def _add_exclude_row(self, path: str) -> None:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit()
        edit.setText(path)
        edit.setPlaceholderText("文件夹路径或图片文件路径…")
        folder_btn = QPushButton("选择文件夹…")
        file_btn = QPushButton("选择图片…")
        del_btn = QPushButton("删除")

        def browse_folder() -> None:
            start = self._dialog_directory_start(edit.text())
            folder = QFileDialog.getExistingDirectory(self, "选择要排除的目录", start)
            if folder:
                self._remember_dialog_directory(folder)
                edit.setText(folder)

        def browse_files() -> None:
            start = self._dialog_directory_start(edit.text())
            if not os.path.isdir(start):
                start = str(Path(start).resolve().parent if os.path.isfile(start) else Path.cwd())
            files, _ = QFileDialog.getOpenFileNames(
                self,
                "选择要排除的图片（可多选）",
                start,
                "图片文件 (*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff);;所有文件 (*.*)",
            )
            if files:
                self._remember_dialog_directory(os.path.dirname(os.path.abspath(files[0])))
                self._apply_sorted_exclude_selection(edit, list(files))

        def remove() -> None:
            self._remove_exclude_row(edit)

        folder_btn.clicked.connect(browse_folder)
        file_btn.clicked.connect(browse_files)
        del_btn.clicked.connect(remove)
        h.addWidget(edit)
        h.addWidget(folder_btn)
        h.addWidget(file_btn)
        h.addWidget(del_btn)
        self._exclude_layout.addWidget(row)
        self._exclude_row_edits.append(edit)

    def _remove_exclude_row(self, edit: QLineEdit) -> None:
        if edit not in self._exclude_row_edits:
            return
        idx = self._exclude_row_edits.index(edit)
        item = self._exclude_layout.takeAt(idx)
        if item.widget():
            item.widget().deleteLater()
        self._exclude_row_edits.pop(idx)
        if not self._exclude_row_edits:
            self._add_exclude_row("")

    def _first_source_hint(self) -> str:
        if self.source_list.count() >= 1:
            return self.source_list.item(0).text().strip()
        return ""

    def _path_row_key(self, path_str: str) -> str:
        try:
            return str(Path(path_str.strip()).resolve()).lower()
        except OSError:
            return os.path.normpath(path_str).lower()

    def _existing_source_keys(self) -> set[str]:
        keys: set[str] = set()
        for i in range(self.source_list.count()):
            t = self.source_list.item(i).text().strip()
            if t:
                keys.add(self._path_row_key(t))
        return keys

    def _append_sources(self, paths: list[str]) -> None:
        new = _normalized_unique_paths(paths)
        if not new:
            return
        seen = self._existing_source_keys()
        for ap in sorted(new, key=lambda fp: tuple(x.lower() for x in Path(fp).parts)):
            k = self._path_row_key(ap)
            if k in seen:
                continue
            seen.add(k)
            self.source_list.addItem(ap)

    def _on_sources_dropped(self, paths: list[str]) -> None:
        n_before = self.source_list.count()
        self._append_sources(paths)
        if paths and self.source_list.count() == n_before:
            QMessageBox.information(
                self,
                "提示",
                "未能添加任何路径。\n可为文件夹或可识别扩展名的图片；已存在的重复项会自动跳过。",
            )

    def _remove_selected_sources(self) -> None:
        rows = sorted({idx.row() for idx in self.source_list.selectedIndexes()}, reverse=True)
        for row in rows:
            self.source_list.takeItem(row)

    def _clear_sources_list(self) -> None:
        self.source_list.clear()

    def select_sources_folder(self) -> None:
        start = self._dialog_directory_start(self._first_source_hint())
        folder = QFileDialog.getExistingDirectory(self, "添加素材文件夹", start)
        if folder:
            self._remember_dialog_directory(folder)
            self._append_sources([folder])

    def select_sources_files(self) -> None:
        start = self._dialog_directory_start(self._first_source_hint())
        if not os.path.isdir(start):
            start = str(Path.cwd())
        pairs = QFileDialog.getOpenFileNames(
            self,
            "添加素材图片（可多选）",
            start,
            "图片 (*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff);;所有文件 (*)",
        )
        files = pairs[0]
        if files:
            self._remember_dialog_directory(os.path.dirname(os.path.abspath(files[0])))
            self._append_sources(files)

    def _collect_excluded_paths(self) -> list[str]:
        return [e.text().strip() for e in self._exclude_row_edits if e.text().strip()]

    def get_config(self) -> AppConfig:
        if self.pad_transparent.isChecked():
            pad = "transparent"
        elif self.pad_white.isChecked():
            pad = "white"
        else:
            pad = "black"
        return AppConfig(
            source_paths=[
                self.source_list.item(i).text().strip()
                for i in range(self.source_list.count())
                if self.source_list.item(i).text().strip()
            ],
            target_width=self.width_input.value(),
            target_height=self.height_input.value(),
            split_long_wide=self.size_mode_split.isChecked(),
            long_target_width=self.long_w_input.value(),
            long_target_height=self.long_h_input.value(),
            wide_target_width=self.wide_w_input.value(),
            wide_target_height=self.wide_h_input.value(),
            use_exclude_dirs=self.exclude_enable.isChecked(),
            excluded_paths=self._collect_excluded_paths(),
            recursive=self.recursive_checkbox.isChecked(),
            resize_images=self.resize_checkbox.isChecked(),
            padding_color=pad,
        )

    def set_config(self, cfg: AppConfig) -> None:
        self.source_list.clear()
        for p in cfg.source_paths:
            pst = str(p).strip()
            if pst:
                self.source_list.addItem(pst)
        self.width_input.setValue(cfg.target_width)
        self.height_input.setValue(cfg.target_height)
        self.long_w_input.setValue(cfg.long_target_width)
        self.long_h_input.setValue(cfg.long_target_height)
        self.wide_w_input.setValue(cfg.wide_target_width)
        self.wide_h_input.setValue(cfg.wide_target_height)
        self.size_mode_unified.blockSignals(True)
        self.size_mode_split.blockSignals(True)
        if cfg.split_long_wide:
            self.size_mode_split.setChecked(True)
        else:
            self.size_mode_unified.setChecked(True)
        self.size_mode_unified.blockSignals(False)
        self.size_mode_split.blockSignals(False)
        self.exclude_enable.setChecked(cfg.use_exclude_dirs)
        self._clear_exclude_rows()
        paths = list(cfg.excluded_paths)
        if not paths:
            self._add_exclude_row("")
        else:
            for p in paths:
                self._add_exclude_row(p)
        self.recursive_checkbox.setChecked(cfg.recursive)
        self.resize_checkbox.setChecked(cfg.resize_images)
        if cfg.padding_color == "transparent":
            self.pad_transparent.setChecked(True)
        elif cfg.padding_color == "white":
            self.pad_white.setChecked(True)
        else:
            self.pad_black.setChecked(True)
        self._update_size_mode_visibility()

    def _refresh_size_preset_combo(self) -> None:
        store = load_presets_store()
        names = sorted(store.get("size_presets", {}).keys())
        self.size_preset_combo.blockSignals(True)
        self.size_preset_combo.clear()
        self.size_preset_combo.addItems(names)
        last = store.get("last_size_preset", "")
        if last in names:
            self.size_preset_combo.setCurrentText(last)
        self.size_preset_combo.blockSignals(False)

    def _current_size_pair(self) -> tuple[int, int]:
        if self.size_mode_split.isChecked():
            return self.long_w_input.value(), self.long_h_input.value()
        return self.width_input.value(), self.height_input.value()

    def apply_selected_size_preset(self) -> None:
        name = self.size_preset_combo.currentText().strip()
        if not name:
            QMessageBox.information(self, "提示", "请先在下拉框中选择一个尺寸比例预设。")
            return
        store = load_presets_store()
        raw = store.get("size_presets", {}).get(name)
        if not isinstance(raw, dict):
            QMessageBox.warning(self, "提示", "该比例预设不存在或已损坏。")
            return
        try:
            w = int(raw["w"])
            h = int(raw["h"])
        except (KeyError, TypeError, ValueError):
            QMessageBox.warning(self, "提示", "比例预设数据无效。")
            return
        if self.size_mode_split.isChecked():
            self.long_w_input.setValue(w)
            self.long_h_input.setValue(h)
            self.wide_w_input.setValue(h)
            self.wide_h_input.setValue(w)
        else:
            self.width_input.setValue(w)
            self.height_input.setValue(h)
        store["last_size_preset"] = name
        save_full_store(store)
        self.append_log(f"已应用尺寸比例: {name} ({w}×{h})")

    def save_size_preset_as(self) -> None:
        w, h = self._current_size_pair()
        name = f"{w}×{h}"
        store = load_presets_store()
        sp = store.get("size_presets", {})
        if not isinstance(sp, dict):
            sp = {}
        if name in sp:
            r = QMessageBox.question(
                self,
                "覆盖",
                f"比例预设「{name}」已存在，是否覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        sp[name] = {"w": w, "h": h}
        store["size_presets"] = sp
        store["last_size_preset"] = name
        save_full_store(store)
        self._refresh_size_preset_combo()
        self.size_preset_combo.setCurrentText(name)
        self.append_log(f"已保存尺寸比例: {name} ({w}×{h})")

    def delete_selected_size_preset(self) -> None:
        name = self.size_preset_combo.currentText().strip()
        store = load_presets_store()
        sp = store.get("size_presets", {})
        if not name or not isinstance(sp, dict) or name not in sp:
            QMessageBox.information(self, "提示", "请先选择一个已有尺寸比例预设。")
            return
        r = QMessageBox.question(
            self,
            "删除",
            f"确定删除尺寸比例预设「{name}」？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        del sp[name]
        store["size_presets"] = sp
        if store.get("last_size_preset") == name:
            store["last_size_preset"] = next(iter(sorted(sp.keys())), "")
        save_full_store(store)
        self._refresh_size_preset_combo()
        self.append_log(f"已删除尺寸比例预设: {name}")

    def _refresh_preset_combo(self) -> None:
        store = load_presets_store()
        names = sorted(store["presets"].keys())
        self.preset_combo.clear()
        self.preset_combo.addItems(names)
        last = store.get("last_preset", "")
        if last in names:
            self.preset_combo.setCurrentText(last)

    def _load_startup_preset(self) -> None:
        store = load_presets_store()
        last = store.get("last_preset", "")
        presets = store.get("presets", {})
        if last and last in presets and isinstance(presets[last], dict):
            try:
                self.set_config(_config_from_dict(presets[last]))
                self.append_log(f"已自动加载预设: {last}")
                return
            except (TypeError, ValueError):
                pass
        self.set_config(AppConfig())

    def load_selected_preset(self) -> None:
        name = self.preset_combo.currentText().strip()
        if not name:
            QMessageBox.information(self, "提示", "请先在下拉框中选择一个预设。")
            return
        store = load_presets_store()
        raw = store["presets"].get(name)
        if not isinstance(raw, dict):
            QMessageBox.warning(self, "提示", "该预设不存在或已损坏。")
            return
        self.set_config(_config_from_dict(raw))
        store["last_preset"] = name
        save_full_store(store)
        self.append_log(f"已加载预设: {name}")

    def save_preset_as(self) -> None:
        name, ok = QInputDialog.getText(self, "保存为预设", "预设名称:")
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "提示", "名称不能为空。")
            return
        store = load_presets_store()
        if name in store["presets"]:
            r = QMessageBox.question(
                self,
                "覆盖",
                f"预设「{name}」已存在，是否覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        store["presets"][name] = asdict(self.get_config())
        store["last_preset"] = name
        save_full_store(store)
        self._refresh_preset_combo()
        self.preset_combo.setCurrentText(name)
        self.append_log(f"已保存预设: {name}")

    def update_current_preset(self) -> None:
        name = self.preset_combo.currentText().strip()
        if not name:
            QMessageBox.information(self, "提示", "请先在预设列表中选择或输入要更新的预设名。")
            return
        store = load_presets_store()
        if name not in store["presets"]:
            r = QMessageBox.question(
                self,
                "新建",
                f"预设「{name}」尚不存在，是否新建？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        store["presets"][name] = asdict(self.get_config())
        store["last_preset"] = name
        save_full_store(store)
        self._refresh_preset_combo()
        self.preset_combo.setCurrentText(name)
        self.append_log(f"已更新预设: {name}")

    def rename_preset(self) -> None:
        old = self.preset_combo.currentText().strip()
        if not old or old not in load_presets_store()["presets"]:
            QMessageBox.information(self, "提示", "请先选择一个已有预设。")
            return
        new_name, ok = QInputDialog.getText(self, "重命名预设", "新名称:", text=old)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            QMessageBox.warning(self, "提示", "名称不能为空。")
            return
        store = load_presets_store()
        if new_name in store["presets"] and new_name != old:
            QMessageBox.warning(self, "提示", "该名称已被占用。")
            return
        data = store["presets"].pop(old)
        store["presets"][new_name] = data
        if store.get("last_preset") == old:
            store["last_preset"] = new_name
        save_full_store(store)
        self._refresh_preset_combo()
        self.preset_combo.setCurrentText(new_name)
        self.append_log(f"预设已重命名: {old} → {new_name}")

    def delete_selected_preset(self) -> None:
        name = self.preset_combo.currentText().strip()
        if not name or name not in load_presets_store()["presets"]:
            QMessageBox.information(self, "提示", "请先选择一个已有预设。")
            return
        r = QMessageBox.question(
            self,
            "删除",
            f"确定删除预设「{name}」？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        store = load_presets_store()
        del store["presets"][name]
        if store.get("last_preset") == name:
            store["last_preset"] = next(iter(sorted(store["presets"].keys())), "")
        save_full_store(store)
        self._refresh_preset_combo()
        self.append_log(f"已删除预设: {name}")

    def _remember_dialog_directory(self, path: str) -> None:
        p = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
        if os.path.isdir(p):
            self._dialog_start_dir = p

    def _dialog_directory_start(self, field_hint: str = "") -> str:
        d = self._dialog_start_dir
        if d and os.path.isdir(d):
            return d
        for s in (field_hint.strip(), self._first_source_hint()):
            if not s:
                continue
            s = os.path.normpath(os.path.abspath(os.path.expanduser(s)))
            if os.path.isdir(s):
                return s
            if os.path.isfile(s):
                parent = os.path.dirname(s)
                if parent and os.path.isdir(parent):
                    return parent
        return str(Path.cwd().resolve())

    def append_log(self, text: str) -> None:
        self.log_box.appendPlainText(text)

    def start_task(self) -> None:
        if self.source_list.count() < 1:
            QMessageBox.warning(self, "提示", "请先添加至少一个文件夹或素材图片（可拖放或多选）。")
            return

        cfg = self.get_config()
        if not cfg.resize_images:
            QMessageBox.warning(self, "提示", "请勾选「等比缩放并补边」。")
            return
        if cfg.use_exclude_dirs:
            bad = [p for p in cfg.excluded_paths if p and not Path(p).exists()]
            if bad:
                QMessageBox.warning(
                    self,
                    "提示",
                    "以下排除路径不存在，请检查:\n" + "\n".join(bad[:8]),
                )
                return
        self.append_log("开始：自动批量处理…")
        self.worker = Worker(cfg)

        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("处理中...")

        self.thread = QThread(self)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self.append_log)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.cleanup_worker)
        self.worker.failed.connect(self.cleanup_worker)
        self.thread.start()

    def cancel_task(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.status_label.setText("取消中...")
            self.append_log("已请求取消，等待当前文件处理完成。")

    def cleanup_worker(self) -> None:
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if self.thread:
            self.thread.quit()
            self.thread.wait()
            self.thread = None
        self.worker = None

    def on_progress(self, current: int, total: int) -> None:
        if total <= 0:
            self.progress_bar.setValue(0)
            return
        self.progress_bar.setValue(int(current * 100 / total))

    def on_finished(self, stats: dict) -> None:
        self.status_label.setText("完成" if not stats["cancelled"] else "已取消")
        self.append_log(
            f"结束: total={stats['total']}, resized={stats['resized']}, "
            f"size_ok={stats['skipped_size_ok']}, errors={stats['errors']}"
        )
        QMessageBox.information(self, "完成", "处理已结束，请查看日志。")

    def on_failed(self, error_msg: str) -> None:
        self.status_label.setText("失败")
        self.append_log(f"[异常] {error_msg}")
        QMessageBox.critical(self, "错误", error_msg)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        store = load_presets_store()
        name = self.preset_combo.currentText().strip()
        if name and name in store.get("presets", {}):
            store["last_preset"] = name
        sz = self.size_preset_combo.currentText().strip()
        sp = store.get("size_presets", {})
        if sz and isinstance(sp, dict) and sz in sp:
            store["last_size_preset"] = sz
        save_full_store(store)
        super().closeEvent(event)


def main() -> None:
    app = QApplication([])
    win = MainWindow()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
