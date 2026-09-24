#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CachyOS GPU Squeezer - Видео конвертер с аппаратным ускорением
ИСПРАВЛЕННАЯ ВЕРСИЯ (без удаления готовых файлов + без задержек)

Запуск БЕЗ КОНСОЛИ: файл перезапускается через pythonw.exe до импорта PyQt,
а если консоль всё же осталась (например, pythonw недоступен), она
гарантированно скрывается средствами Windows API (FreeConsole).
Флаг VSQUIEZER_NO_CONSOLE защищает от бесконечного цикла перезапусков.
"""

import sys  # используется в блоке авто-перезапуска ниже (os импортируется следом)

# ============================================================================
# АБСОЛЮТНО ПЕРВЫЙ БЛОК: гарантированный старт БЕЗ КОНСОЛИ (только Windows)
# ----------------------------------------------------------------------------
# Если файл запускается двойным кликом через python.exe (или из cmd), то к
# моменту импорта этого модуля консольное окно УЖЕ создано операционной
# системой, и позже спрятать его нельзя. Поэтому перезапуск через
# pythonw.exe выполняется здесь — до импорта PyQt и любых других модулей,
# чтобы чёрное окно консоли не мелькало вообще.
# Для собранного EXE (PyInstaller --noconsole / cx_Freeze BASE="Win32")
# консоли нет по построению — код ниже его не создаёт и не трогает.
# Флаг VSQUIEZER_NO_CONSOLE защищает от бесконечного цикла перезапусков.
# ============================================================================
if sys.platform == "win32" and not getattr(sys, "frozen", False):
    try:
        import os as _os
        if _os.environ.get("VSQUIEZER_NO_CONSOLE") != "1":
            _script = _os.path.abspath(__file__)
            # .py -> .pyw: association launches the GUI interpreter directly
            if _script.lower().endswith(".py"):
                _pyw = _script[:-3] + ".pyw"
                if _os.path.exists(_pyw):
                    _os.environ["VSQUIEZER_NO_CONSOLE"] = "1"
                    _os.execv(sys.executable, [sys.executable, _pyw] + sys.argv[1:])
            _base = _os.path.basename(sys.executable).lower()
            if _base in ("python.exe", "python3.exe"):
                _pythonw = _os.path.join(_os.path.dirname(sys.executable), "pythonw.exe")
                if _os.path.exists(_pythonw):
                    _os.environ["VSQUIEZER_NO_CONSOLE"] = "1"
                    _os.execv(_pythonw, [_pythonw, _script] + sys.argv[1:])
    except Exception:
        # Если перезапуск не удался — продолжаем работу в текущем процессе
        pass

import os
import sys
import stat
import shutil
import zipfile
import tarfile
import subprocess
import re
import time
import logging
import tempfile
import traceback
import threading
import ctypes
import urllib.request
from PyQt6.QtWidgets import (QMainWindow, QApplication, QTableWidgetItem, QTableWidget, QProgressBar,
                             QFileDialog, QLabel, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLineEdit, QHeaderView, QAbstractItemView,
                             QComboBox, QSlider, QCheckBox, QDialog, QMessageBox)
from PyQt6.QtCore import (QObject, pyqtSignal, QSettings, QUrl, Qt, QTimer,
                          QRect, QPoint, QSize, QThread)
from PyQt6.QtGui import (QIcon, QImage, QPainter, QColor, QBrush, QPen, QPixmap)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink, QVideoFrame

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================
def get_exe_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

EXE_DIR = get_exe_dir()
CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# ============================================================================
# АВТОМАТИЧЕСКОЕ СКАЧИВАНИЕ НЕДОСТАЮЩИХ КОМПОНЕНТОВ (ffmpeg / ffprobe)
# ============================================================================
FFMPEG_WIN_URL = ("https://www.gyan.dev/ffmpeg/builds/"
                  "ffmpeg-release-essentials.zip")
FFMPEG_LINUX_URLS = [
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz",
]


def _make_executable(path):
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass


def _find_binary(root, names):
    """Рекурсивно ищет исполняемые файлы с указанными именами внутри распакованной архива."""
    found = {}
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            if fname in names and fname not in found:
                found[fname] = os.path.join(dirpath, fname)
        if len(found) == len(names):
            break
    return found


def _download_file(url, dest, reporthook=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (GPU-Squeezer)"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length", "0"))
        block_size = 1024 * 256
        while True:
            chunk = resp.read(block_size)
            if not chunk:
                break
            out.write(chunk)
            if reporthook:
                reporthook(out.tell(), block_size, total)


def _extract_archive(archive_path, extract_dir):
    if archive_path.lower().endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_dir)
    elif archive_path.endswith((".tar.xz", ".tar.gz")):
        mode = "r:xz" if archive_path.endswith(".tar.xz") else "r:gz"
        with tarfile.open(archive_path, mode) as tf:
            tf.extractall(extract_dir)
    else:
        raise ValueError(f"Неизвестный формат архива: {archive_path}")


def ensure_components(progress_cb=None, status_cb=None):
    """
    Проверяет наличие ffmpeg/ffprobe при старте и скачивает недостающие.
    progress_cb(procents), status_cb(текст). Возвращает список проблем (пуст если всё ок).
    """
    problems = []
    binaries = ("ffmpeg.exe", "ffprobe.exe") if sys.platform == "win32" else ("ffmpeg", "ffprobe")

    def binary_ok(path):
        if not path or not os.path.exists(path):
            return False
        try:
            r = subprocess.run([path, "-version"], capture_output=True,
                               creationflags=CREATION_FLAGS, timeout=15)
            return r.returncode == 0
        except Exception:
            return False

    # 1. Локальные файлы рядом с программой
    local_ok = all(binary_ok(os.path.join(EXE_DIR, b)) for b in binaries)
    if local_ok:
        logging.info("Компоненты ffmpeg/ffprobe найдены локально — скачивание не требуется.")
        return problems

    # 2. Системный PATH
    if all(shutil.which(b.replace(".exe", "")) for b in binaries):
        logging.info("Компоненты найдены в системе (PATH) — скачивание не требуется.")
        return problems

    # 3. Скачиваем portable-сборку
    urls = [FFMPEG_WIN_URL] if sys.platform == "win32" else FFMPEG_LINUX_URLS
    last_err = None
    for url in urls:
        try:
            if status_cb:
                status_cb(f"Скачивание компонентов:\n{url}")
            tmp_dir = tempfile.mkdtemp(prefix="squeezer_dl_")
            archive_name = "ffmpeg_archive" + (".zip" if url.endswith(".zip")
                                               else (".tar.xz" if url.endswith(".tar.xz") else ".tar.gz"))
            archive_path = os.path.join(tmp_dir, archive_name)
            _download_file(url, archive_path, reporthook=lambda n, bs, tot:
                           progress_cb(min(90, int(n * bs * 100 / tot))) if progress_cb and tot > 0 else None)
            if progress_cb:
                progress_cb(92)
            if status_cb:
                status_cb("Распаковка компонентов...")
            extract_dir = os.path.join(tmp_dir, "extracted")
            _extract_archive(archive_path, extract_dir)
            if progress_cb:
                progress_cb(97)
            found = _find_binary(extract_dir, set(binaries))
            if len(found) != len(binaries):
                raise RuntimeError("В архиве не найдены нужные исполняемые файлы")
            for name, src in found.items():
                dst = os.path.join(EXE_DIR, name)
                shutil.copy2(src, dst)
                _make_executable(dst)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if all(binary_ok(os.path.join(EXE_DIR, b)) for b in binaries):
                logging.info(f"Компоненты успешно скачаны в {EXE_DIR}")
                if progress_cb:
                    progress_cb(100)
                return problems
            raise RuntimeError("Скачанные файлы не запускаются")
        except Exception as e:
            last_err = e
            logging.error(f"Не удалось скачать компоненты из {url}: {e}")
            shutil.rmtree(tmp_dir, ignore_errors=True) if 'tmp_dir' in locals() and os.path.exists(tmp_dir) else None

    problems.append(f"Автоматическое скачивание не удалось: {last_err}. "
                    f"Установите ffmpeg вручную или положите ffmpeg/ffprobe рядом с программой.")
    return problems

def get_short_path(long_path):
    if sys.platform != "win32":
        return long_path
    try:
        buffer = ctypes.create_unicode_buffer(260)
        result = ctypes.windll.kernel32.GetShortPathNameW(long_path, buffer, 260)
        if result > 0:
            short = buffer.value
            if short and os.path.exists(short):
                return short
    except Exception as e:
        logging.warning(f"Не удалось получить короткий путь: {e}")
    return long_path

if sys.platform == "win32":
    FFMPEG_LONG = os.path.join(EXE_DIR, "ffmpeg.exe") if os.path.exists(os.path.join(EXE_DIR, "ffmpeg.exe")) else None
    FFPROBE_LONG = os.path.join(EXE_DIR, "ffprobe.exe") if os.path.exists(os.path.join(EXE_DIR, "ffprobe.exe")) else None
else:
    FFMPEG_LONG = os.path.join(EXE_DIR, "ffmpeg") if os.path.exists(os.path.join(EXE_DIR, "ffmpeg")) else None
    FFPROBE_LONG = os.path.join(EXE_DIR, "ffprobe") if os.path.exists(os.path.join(EXE_DIR, "ffprobe")) else None

if not FFMPEG_LONG:
    FFMPEG_LONG = "ffmpeg"
if not FFPROBE_LONG:
    FFPROBE_LONG = "ffprobe"

FFMPEG_EXE = get_short_path(FFMPEG_LONG)
FFPROBE_EXE = get_short_path(FFPROBE_LONG)
EXE_DIR_SHORT = get_short_path(EXE_DIR)

log_path = os.path.join(os.path.expanduser("~"), "squeezer_debug.log")
logging.basicConfig(filename=log_path, filemode='a',
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    level=logging.DEBUG, encoding='utf-8')
logging.info(f"Приложение запущено")
logging.info(f"FFMPEG_EXE: {FFMPEG_EXE}")
logging.info(f"FFPROBE_EXE: {FFPROBE_EXE}")

def exception_hook(exctype, value, tb):
    logging.error("КРИТИЧЕСКАЯ ОШИБКА: " + "".join(traceback.format_exception(exctype, value, tb)))
    sys.__excepthook__(exctype, value, tb)
sys.excepthook = exception_hook

CACHYOS_STYLESHEET = """
QMainWindow { background-color: #1e222a; }
QWidget { color: #fcfff5; font-family: 'Noto Sans', sans-serif; font-size: 13px; }
QTableWidget { background-color: #21262e; border: 1px solid #111418; gridline-color: #2c323c; border-radius: 6px; }
QTableWidget::item { padding: 5px; }
QTableWidget::item:selected { background-color: #00aa7f; color: #ffffff; }
QHeaderView::section { background-color: #181b20; color: #00ffbc; padding: 6px; border: 1px solid #111418; font-weight: bold; }
QPushButton { background-color: #2c323c; border: 1px solid #3f4754; border-radius: 5px; padding: 6px 12px; }
QPushButton:hover { background-color: #3f4754; border-color: #00ffbc; }
QLineEdit, QComboBox { background-color: #181b20; border: 1px solid #3f4754; border-radius: 4px; padding: 4px; color: #00ffbc; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #00ffbc; }
QComboBox::drop-down { border: none; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #3f4754; border-radius: 3px; background: #181b20; }
QCheckBox::indicator:checked { background: #00aa7f; border-color: #00ffbc; }
QProgressBar { border: 1px solid #111418; border-radius: 4px; background-color: #181b20; text-align: center; color: #ffffff; font-weight: bold; }
QProgressBar::chunk { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00aa7f, stop:1 #00ffbc); border-radius: 3px; }
QSlider::groove:horizontal { border: 1px solid #111418; height: 4px; background: #181b20; }
QSlider::handle:horizontal { background: #00ffbc; width: 12px; margin: -4px 0; border-radius: 6px; }
QLabel { color: #abb2bf; }
"""

# ============================================================================
# ВИДЖЕТЫ
# ============================================================================
class VisualRangeSlider(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(30)
        self.duration = 1000
        self.start_pos = 0
        self.end_pos = 1000
        self.current_pos = 0
        self.active_thumb = None
        self.on_range_changed = None
        self.on_position_changed = None

    def set_duration(self, duration_ms):
        self.duration = max(1, duration_ms)
        self.update()

    def set_values(self, start_ms, end_ms, current_ms):
        if end_ms > self.duration:
            self.duration = end_ms
        self.start_pos = max(0, min(start_ms, self.duration))
        self.end_pos = max(self.start_pos, min(end_ms, self.duration))
        self.current_pos = max(0, min(current_ms, self.duration))
        self.update()

    def _ms_to_x(self, ms):
        width = self.width() - 20
        if width <= 0: return 10
        return int(10 + (ms / self.duration) * width)

    def _x_to_ms(self, x):
        width = self.width() - 20
        if width <= 0: return 0
        val = ((x - 10) / width) * self.duration
        return max(0, min(int(val), self.duration))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cy = h // 2
        x_start = self._ms_to_x(self.start_pos)
        x_end = self._ms_to_x(self.end_pos)
        x_curr = self._ms_to_x(self.current_pos)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#181b20")))
        painter.drawRoundedRect(10, cy - 3, max(1, w - 20), 6, 3, 3)

        painter.setBrush(QBrush(QColor("#00aa7f")))
        painter.drawRoundedRect(x_start, cy - 3, max(1, x_end - x_start), 6, 3, 3)

        painter.setBrush(QBrush(QColor("#00ffbc")))
        painter.drawEllipse(x_start - 6, cy - 10, 12, 20)
        painter.drawEllipse(x_end - 6, cy - 10, 12, 20)

        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(QPen(QColor("#1e222a"), 2))
        painter.drawEllipse(x_curr - 7, cy - 7, 14, 14)

    def mousePressEvent(self, event):
        x = event.position().x()
        if abs(x - self._ms_to_x(self.current_pos)) < 10:
            self.active_thumb = 'current'
        elif abs(x - self._ms_to_x(self.start_pos)) < 10:
            self.active_thumb = 'start'
        elif abs(x - self._ms_to_x(self.end_pos)) < 10:
            self.active_thumb = 'end'
        else:
            self.active_thumb = 'current'
        ms = self._x_to_ms(x)
        self.current_pos = ms
        if self.on_position_changed:
            self.on_position_changed(ms)
        self.update()

    def mouseMoveEvent(self, event):
        if not self.active_thumb: return
        x = event.position().x()
        ms = self._x_to_ms(x)
        if self.active_thumb == 'start':
            self.start_pos = min(ms, self.end_pos - 100)
            if self.on_range_changed:
                self.on_range_changed(self.start_pos, self.end_pos)
        elif self.active_thumb == 'end':
            self.end_pos = max(ms, self.start_pos + 100)
            if self.on_range_changed:
                self.on_range_changed(self.start_pos, self.end_pos)
        elif self.active_thumb == 'current':
            self.current_pos = ms
            if self.on_position_changed:
                self.on_position_changed(ms)
        self.update()

    def mouseReleaseEvent(self, event):
        self.active_thumb = None


# ============================================================================
# АСИНХРОННЫЙ ЗАГРУЗЧИК МЕТАДАННЫХ (чтобы не было задержек)
# ============================================================================
class VideoLoaderThread(QThread):
    """Поток для получения метаданных видео без блокировки GUI"""
    metadata_ready = pyqtSignal(str, int, int, int)  # path, width, height, duration_ms

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        try:
            cmd_res = [FFPROBE_EXE, "-v", "error", "-select_streams", "v:0",
                      "-show_entries", "stream=width,height", "-of", "default=nw=1:nk=1", self.path]
            res_output = subprocess.check_output(cmd_res, stderr=subprocess.DEVNULL,
                                                 creationflags=CREATION_FLAGS, timeout=10).decode().strip()
            numbers = re.findall(r'\d+', res_output)
            width = int(numbers[0]) if len(numbers) >= 2 else 1920
            height = int(numbers[1]) if len(numbers) >= 2 else 1080

            cmd_dur = [FFPROBE_EXE, "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=nw=1:nk=1", self.path]
            dur_output = subprocess.check_output(cmd_dur, stderr=subprocess.DEVNULL,
                                                 creationflags=CREATION_FLAGS, timeout=10).decode().strip()
            duration_ms = 0
            if dur_output:
                try:
                    duration_ms = int(float(dur_output) * 1000)
                except ValueError:
                    pass

            self.metadata_ready.emit(self.path, width, height, duration_ms)
        except Exception as e:
            logging.error(f"Ошибка в VideoLoaderThread: {e}")
            self.metadata_ready.emit(self.path, 1920, 1080, 0)


class SqueezerPlayer(QWidget):
    def __init__(self, range_callback, parent=None):
        super().__init__(parent)
        self.video_canvas = QWidget(self)
        self.video_canvas.setStyleSheet("background-color: #000000; border: 1px solid #2c323c; border-radius: 6px;")
        self.video_canvas.paintEvent = self._paint_video_frame
        self.current_frame = QImage()
        self.current_real_crop = None
        self.video_real_size = (1920, 1080)
        self._loader_thread = None

        self.media_player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.media_player.setAudioOutput(self.audio_output)
        self.video_sink = QVideoSink()
        self.media_player.setVideoSink(self.video_sink)
        self.video_sink.videoFrameChanged.connect(self._on_frame_changed)

        self.slider = VisualRangeSlider()
        self.slider.on_position_changed = self.scrub_to_position
        self.slider.on_range_changed = range_callback

        self.media_player.positionChanged.connect(self.on_player_position_changed)
        self.media_player.durationChanged.connect(self._on_duration_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video_canvas, stretch=1)
        layout.addWidget(self.slider)

    def _on_duration_changed(self, duration_ms):
        if duration_ms > 0:
            self.slider.set_duration(duration_ms)
            self.slider.set_values(0, duration_ms, 0)

    def _on_frame_changed(self, frame: QVideoFrame):
        if frame.isValid():
            if frame.map(QVideoFrame.MapMode.ReadOnly):
                image = frame.toImage()
                self.current_frame = image.convertToFormat(QImage.Format.Format_RGB32)
                frame.unmap()
                self.video_canvas.update()

    def _paint_video_frame(self, event):
        painter = QPainter(self.video_canvas)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        canvas_rect = self.video_canvas.rect()
        if self.current_frame.isNull():
            painter.fillRect(canvas_rect, QColor("#000000"))
            return
        if not self.current_real_crop:
            scaled_size = QSize(self.current_frame.width(), self.current_frame.height())
            scaled_size.scale(canvas_rect.size(), Qt.AspectRatioMode.KeepAspectRatio)
            x = (canvas_rect.width() - scaled_size.width()) // 2
            y = (canvas_rect.height() - scaled_size.height()) // 2
            target_rect = QRect(x, y, scaled_size.width(), scaled_size.height())
            painter.drawImage(target_rect, self.current_frame, self.current_frame.rect())
        else:
            try:
                crop_w, crop_h, crop_x, crop_y = map(int, self.current_real_crop.split(':'))
                source_rect = QRect(crop_x, crop_y, crop_w, crop_h)
                scaled_crop_size = QSize(crop_w, crop_h)
                scaled_crop_size.scale(canvas_rect.size(), Qt.AspectRatioMode.KeepAspectRatio)
                cx = (canvas_rect.width() - scaled_crop_size.width()) // 2
                cy = (canvas_rect.height() - scaled_crop_size.height()) // 2
                target_rect = QRect(cx, cy, scaled_crop_size.width(), scaled_crop_size.height())
                painter.drawImage(target_rect, self.current_frame, source_rect)
            except Exception as e:
                logging.error(f"Ошибка отрисовки кропа: {e}")
                painter.drawImage(canvas_rect, self.current_frame)

    def load_video(self, path):
        """АСИНХРОННАЯ загрузка — не блокирует GUI"""
        if not path or not os.path.exists(path):
            return
        try:
            self.slider.start_pos = 0
            self.slider.end_pos = 1000
            self.current_real_crop = None
            self.current_frame = QImage()

            if self._loader_thread and self._loader_thread.isRunning():
                self._loader_thread.quit()
                self._loader_thread.wait(1000)

            self._loader_thread = VideoLoaderThread(path)
            self._loader_thread.metadata_ready.connect(self._on_metadata_loaded)
            self._loader_thread.start()

            self.media_player.setSource(QUrl.fromLocalFile(path))
            self.media_player.play()
            self.media_player.pause()

            self.video_canvas.update()
        except Exception as e:
            logging.error(f"Ошибка load_video: {e}")

    def _on_metadata_loaded(self, path, width, height, duration_ms):
        try:
            current_source = self.media_player.source().toString()
            if path not in current_source:
                return
        except Exception:
            pass
        self.video_real_size = (width, height)
        if duration_ms > 0:
            self.slider.set_duration(duration_ms)
            self.slider.set_values(0, duration_ms, 0)

    def update_crop_mask(self, crop_str):
        self.current_real_crop = crop_str if crop_str else None
        self.video_canvas.update()

    def toggle_play(self):
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
        else:
            self.media_player.play()

    def scrub_to_position(self, position_ms):
        self.media_player.setPosition(position_ms)

    def on_player_position_changed(self, position):
        if self.slider.active_thumb:
            return
        self.slider.set_values(self.slider.start_pos, self.slider.end_pos, position)


class ImageCropLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.crop_rect = QRect(50, 50, 200, 200)
        self.drag_start = QPoint()
        self.is_dragging = False
        self.is_resizing = False
        self.resize_margin = 20
        self.setMouseTracking(True)
        self.has_image_loaded = False

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.has_image_loaded or not self.pixmap(): return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cr = self.contentsRect()
        pm_size = self.pixmap().size()
        x_off = cr.left() + (cr.width() - pm_size.width()) // 2
        y_off = cr.top() + (cr.height() - pm_size.height()) // 2
        img_rect = QRect(x_off, y_off, pm_size.width(), pm_size.height())

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 160)))
        top_h = max(0, self.crop_rect.top() - img_rect.top())
        left_w = max(0, self.crop_rect.left() - img_rect.left())
        right_w = max(0, img_rect.right() - self.crop_rect.right())
        bottom_h = max(0, img_rect.bottom() - self.crop_rect.bottom())
        painter.drawRect(img_rect.left(), img_rect.top(), img_rect.width(), top_h)
        painter.drawRect(img_rect.left(), self.crop_rect.top(), left_w, self.crop_rect.height())
        painter.drawRect(self.crop_rect.right(), self.crop_rect.top(), right_w, self.crop_rect.height())
        painter.drawRect(img_rect.left(), self.crop_rect.bottom(), img_rect.width(), bottom_h)

        pen = QPen(QColor("#00ffbc"), 3, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.crop_rect)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#00ffbc")))
        painter.drawEllipse(self.crop_rect.right() - 6, self.crop_rect.bottom() - 6, 12, 12)

    def mousePressEvent(self, event):
        if not self.has_image_loaded: return
        pos = event.position().toPoint()
        corner = QPoint(self.crop_rect.right(), self.crop_rect.bottom())
        if (pos - corner).manhattanLength() < self.resize_margin * 2:
            self.is_resizing = True
            event.accept()
        elif self.crop_rect.contains(pos):
            self.is_dragging = True
            self.drag_start = pos - self.crop_rect.topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if not self.has_image_loaded or not self.pixmap(): return
        pos = event.position().toPoint()
        corner = QPoint(self.crop_rect.right(), self.crop_rect.bottom())
        cr = self.contentsRect()
        pm_size = self.pixmap().size()
        x_off = cr.left() + (cr.width() - pm_size.width()) // 2
        y_off = cr.top() + (cr.height() - pm_size.height()) // 2
        img_rect = QRect(x_off, y_off, pm_size.width(), pm_size.height())

        if (pos - corner).manhattanLength() < self.resize_margin * 2:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif self.crop_rect.contains(pos):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

        if self.is_resizing:
            w = max(40, pos.x() - self.crop_rect.left())
            h = max(40, pos.y() - self.crop_rect.top())
            if self.crop_rect.left() + w > img_rect.right():
                w = img_rect.right() - self.crop_rect.left()
            if self.crop_rect.top() + h > img_rect.bottom():
                h = img_rect.bottom() - self.crop_rect.top()
            self.crop_rect.setSize(QSize(w, h))
            self.update()
        elif self.is_dragging:
            new_top_left = pos - self.drag_start
            new_rect = QRect(new_top_left, self.crop_rect.size())
            if new_rect.left() < img_rect.left(): new_rect.moveLeft(img_rect.left())
            if new_rect.top() < img_rect.top(): new_rect.moveTop(img_rect.top())
            if new_rect.right() > img_rect.right(): new_rect.moveRight(img_rect.right())
            if new_rect.bottom() > img_rect.bottom(): new_rect.moveBottom(img_rect.bottom())
            self.crop_rect = new_rect
            self.update()

    def mouseReleaseEvent(self, event):
        self.is_resizing = False
        self.is_dragging = False
        self.setCursor(Qt.CursorShape.ArrowCursor)


class CropDialog(QDialog):
    def __init__(self, video_path, current_ms, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Кадрирование видео (Crop)")
        self.resize(800, 600)
        self.setStyleSheet("background-color: #1e222a; color: #fcfff5;")
        self.video_path = video_path
        self.current_ms = current_ms
        self.crop_area_str = ""
        self.tmp_img = None
        layout = QVBoxLayout(self)
        self.crop_label = ImageCropLabel()
        self.crop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.crop_label, stretch=1)

        btn_layout = QHBoxLayout()
        self.btn_ok = QPushButton("Применить")
        self.btn_ok.setStyleSheet("background-color: #00aa7f; color: white; font-weight: bold; padding: 6px 15px;")
        self.btn_ok.clicked.connect(self.accept_crop)
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setStyleSheet("background-color: #2c323c; padding: 6px 15px;")
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_ok)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        self.load_video_frame()

    def load_video_frame(self):
        self.tmp_img = os.path.join(tempfile.gettempdir(), "squeezer_crop_tmp.png")
        ss_sec = self.current_ms / 1000.0
        try:
            cmd = [FFMPEG_EXE, "-y", "-ss", str(ss_sec), "-i", self.video_path,
                  "-vframes", "1", "-q:v", "2", self.tmp_img]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          check=True, creationflags=CREATION_FLAGS)
            if os.path.exists(self.tmp_img):
                pixmap = QPixmap(self.tmp_img)
                if pixmap.isNull() or pixmap.width() == 0 or pixmap.height() == 0:
                    raise ValueError("Пустой кадр")
                scaled_pixmap = pixmap.scaled(760, 500, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation)
                self.crop_label.setPixmap(scaled_pixmap)
                self.crop_label.has_image_loaded = True
                cr = self.crop_label.contentsRect()
                x_off = cr.left() + (cr.width() - scaled_pixmap.width()) // 2
                y_off = cr.top() + (cr.height() - scaled_pixmap.height()) // 2
                self.crop_label.crop_rect = QRect(x_off + 40, y_off + 40,
                                                  scaled_pixmap.width() - 80,
                                                  scaled_pixmap.height() - 80)
                self.crop_label.update()
        except Exception as e:
            logging.error(f"Ошибка создания превью: {e}")
            self.crop_label.setText("Не удалось загрузить кадр для кадрирования.")

    def accept_crop(self):
        if not self.crop_label.has_image_loaded or not self.crop_label.pixmap():
            self.reject()
            return
        rect = self.crop_label.crop_rect
        pixmap_size = self.crop_label.pixmap().size()
        if pixmap_size.width() == 0 or pixmap_size.height() == 0:
            self.reject()
            return

        cr = self.crop_label.contentsRect()
        x_off = cr.left() + (cr.width() - pixmap_size.width()) // 2
        y_off = cr.top() + (cr.height() - pixmap_size.height()) // 2

        try:
            cmd = [FFPROBE_EXE, "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=width,height", "-of", "default=nw=1:nk=1",
                  self.video_path]
            output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL,
                                             creationflags=CREATION_FLAGS).decode().strip()
            numbers = re.findall(r'\d+', output)
            video_w, video_h = int(numbers[0]), int(numbers[1])
        except Exception:
            video_w, video_h = 1920, 1080

        relative_x = rect.left() - x_off
        relative_y = rect.top() - y_off
        scale_x = video_w / pixmap_size.width()
        scale_y = video_h / pixmap_size.height()
        real_w = int(rect.width() * scale_x)
        real_h = int(rect.height() * scale_y)
        real_x = int(relative_x * scale_x)
        real_y = int(relative_y * scale_y)

        real_x = max(0, min(real_x, video_w))
        real_y = max(0, min(real_y, video_h))
        real_w = max(40, min(real_w, video_w - real_x))
        real_h = max(40, min(real_h, video_h - real_y))
        real_w = real_w - (real_w % 2)
        real_h = real_h - (real_h % 2)
        real_x = real_x - (real_x % 2)
        real_y = real_y - (real_y % 2)

        self.crop_area_str = f"{real_w}:{real_h}:{real_x}:{real_y}"
        self._cleanup()
        self.accept()

    def _cleanup(self):
        if self.tmp_img and os.path.exists(self.tmp_img):
            try: os.remove(self.tmp_img)
            except Exception: pass

    def reject(self):
        self._cleanup()
        super().reject()


# ============================================================================
# ЯДРО С КЕШИРОВАНИЕМ FFPROBE
# ============================================================================
class SqueezerCore(QObject):
    progress_updated = pyqtSignal(int, int, int)
    encode_finished = pyqtSignal(int, int)
    encode_failed = pyqtSignal(int)

    def __init__(self, main_app):
        super().__init__()
        self.app = main_app
        self.ffmpeg_proc = None
        self.start_time = 0.0
        self._probe_cache = {}
        # Флаг глобальной остановки: когда True — текущее видео прерывается,
        # а оставшаяся очередь помечается остановленной (останавливаются ВСЕ сразу)
        self.stop_requested = False

    def _probe(self, path, args_key, cmd):
        """Кешируемый вызов ffprobe — повторные вызовы мгновенные"""
        try:
            mtime = os.path.getmtime(path) if os.path.exists(path) else 0
        except Exception:
            mtime = 0
        cache_key = (path, args_key, mtime)
        if cache_key in self._probe_cache:
            return self._probe_cache[cache_key]
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL,
                                             creationflags=CREATION_FLAGS, timeout=15).decode().strip()
            self._probe_cache[cache_key] = output
            return output
        except Exception as e:
            logging.error(f"Ошибка ffprobe ({args_key}): {e}")
            return ""

    def get_video_duration(self, path):
        cmd = [FFPROBE_EXE, "-v", "error", "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", path]
        output = self._probe(path, "duration", cmd)
        try:
            return float(output)
        except (ValueError, TypeError):
            return 0.0

    def get_video_resolution(self, path):
        cmd = [FFPROBE_EXE, "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height", "-of", "default=nw=1:nk=1", path]
        output = self._probe(path, "resolution", cmd)
        numbers = re.findall(r'\d+', output)
        return f"{numbers[0]}x{numbers[1]}" if len(numbers) >= 2 else "1920x1080"

    def get_source_bitrate(self, path):
        cmd = [FFPROBE_EXE, "-v", "error", "-show_entries", "format=bit_rate",
              "-of", "default=noprint_wrappers=1:nokey=1", path]
        output = self._probe(path, "bitrate", cmd)
        try:
            return int(int(output) / 1000)
        except (ValueError, TypeError):
            return 6500

    def calculate_bitrate(self, row, strict_limit, target_size_gb, global_val):
        if row not in self.app.queue_data:
            return None
        data = self.app.queue_data[row]
        duration = data["end"] - data["start"]
        if duration <= 0:
            return None

        # "Родной" битрейт источника (кб/с) — нужен для проверки лимита размера
        try:
            source_bitrate = int(data.get("source_bitrate") or self.get_source_bitrate(data["path"]))
        except Exception:
            source_bitrate = 0
        data["source_bitrate"] = source_bitrate

        if strict_limit:
            # Запас 5%
            safe_target_gb = max(0.05, target_size_gb * 0.95)
            target_bits = safe_target_gb * 1024 * 1024 * 1024 * 8
            # Правильный расчет: вычитаем аудио (128 кб/с)
            audio_bits_total = 128000 * duration
            video_bits_available = target_bits - audio_bits_total
            calculated_video_bitrate = int(video_bits_available / 1000 / duration)
            calculated_video_bitrate = max(100, calculated_video_bitrate)

            if source_bitrate > 0 and calculated_video_bitrate >= source_bitrate:
                # ИСПРАВЛЕНИЕ: ролик с родным битрейтом весит МЕНЬШЕ лимита —
                # искусственно повышать битрейт нельзя, оставляем родной.
                video_bitrate = source_bitrate
                status_text = f"{video_bitrate} кб/с (родной, в лимите {target_size_gb} ГБ)"
            else:
                # Ролик тяжелее лимита — понижаем битрейт до расчётного
                video_bitrate = calculated_video_bitrate
                status_text = f"{video_bitrate} кб/с (Строго до {target_size_gb} ГБ)"

        elif data.get("custom_enabled"):
            try: video_bitrate = int(data["custom_val"])
            except Exception: video_bitrate = 6500
            status_text = f"{video_bitrate} кб/с"
        else:
            try: video_bitrate = int(global_val)
            except Exception: video_bitrate = 6500
            status_text = f"{video_bitrate} кб/с"

        return video_bitrate, status_text

    def _has_nvidia_gpu(self):
        try:
            subprocess.run(["nvidia-smi"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          check=True, creationflags=CREATION_FLAGS)
            return True
        except Exception:
            return False

    def run_ffmpeg_process(self, row, custom_save_dir):
        try:
            if self.stop_requested:
                return False
            if row not in self.app.queue_data:
                self.encode_failed.emit(row)
                return False
            data = self.app.queue_data[row]

            if custom_save_dir:
                base_name = os.path.basename(data["path"])
                name_without_ext, _ = os.path.splitext(base_name)
                out = os.path.join(custom_save_dir, f"{name_without_ext}_squeezed.mp4")
            else:
                name_without_ext, _ = os.path.splitext(data["path"])
                out = f"{name_without_ext}_squeezed.mp4"
            out = os.path.normpath(out)

            self.app.current_output_file = out
            logging.info(f"Выходной файл: {out}")

            vf_filters = []
            if data.get("crop_area") and data["crop_area"] != " ":
                vf_filters.append(f"crop={data['crop_area']}")
            if data.get("target_res") and data["target_res"] != "Оригинал":
                try:
                    w, h = data["target_res"].split('x')
                    vf_filters.append(f"format=yuv420p,scale={int(w)}:{int(h)}")
                    logging.info(f"Применено разрешение: {data['target_res']}")
                except Exception as e:
                    logging.error(f"Ошибка применения разрешения: {e}")

            args = [FFMPEG_EXE, "-y"]

            if self._has_nvidia_gpu():
                args.extend(["-hwaccel", "cuda",
                            "-ss", str(data["start"]), "-to", str(data["end"]), "-i", data["path"]])
                if vf_filters:
                    args.extend(["-vf", ", ".join(vf_filters)])
                # cbr вместо vbr для строгого контроля размера
                args.extend(["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq",
                            "-rc", "cbr", "-b:v", f"{data['bitrate']}k",
                            "-maxrate", f"{data['bitrate']}k", "-bufsize", f"{data['bitrate'] * 2}k"])
            else:
                args.extend(["-ss", str(data["start"]), "-to", str(data["end"]),
                            "-i", data["path"]])
                if vf_filters:
                    args.extend(["-vf", ", ".join(vf_filters)])
                # maxrate и bufsize для контроля размера
                args.extend(["-c:v", "libx264", "-b:v", f"{data['bitrate']}k",
                            "-maxrate", f"{data['bitrate']}k", "-bufsize", f"{data['bitrate'] * 2}k",
                            "-preset", "medium"])

            args.extend(["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out])

            logging.info(f"Запуск FFmpeg:")
            logging.info(f"  FFMPEG_EXE: {FFMPEG_EXE}")
            logging.info(f"  CWD: {EXE_DIR_SHORT}")
            logging.info(f"  Аргументы: {' '.join(args[1:])}")

            self.start_time = time.time()
            try:
                self.ffmpeg_proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=CREATION_FLAGS,
                    cwd=EXE_DIR_SHORT
                )
                logging.info(f"FFmpeg запущен, PID: {self.ffmpeg_proc.pid}")
            except Exception as e:
                logging.error(f"Не удалось запустить FFmpeg: {e}")
                logging.error(traceback.format_exc())
                QMessageBox.critical(self.app, "Ошибка запуска FFmpeg", str(e))
                self.encode_failed.emit(row)
                return False

            self._progress_thread = threading.Thread(target=self._read_ffmpeg_progress, args=(row,), daemon=True)
            self._progress_thread.start()
            return True
        except Exception as e:
            logging.error(f"Критическая ошибка запуска FFmpeg: {e}")
            logging.error(traceback.format_exc())
            self.encode_failed.emit(row)
            return False

    def _read_ffmpeg_progress(self, row):
        try:
            if not self.ffmpeg_proc:
                return
            stderr_buffer = ""
            stderr_lines = []
            while True:
                byte = self.ffmpeg_proc.stderr.read(1)
                if not byte:
                    break
                char = byte.decode('utf-8', errors='ignore')
                stderr_buffer += char
                if char in ['\n', '\r']:
                    line_str = stderr_buffer.strip()
                    if line_str:
                        stderr_lines.append(line_str)
                    match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line_str)
                    if match:
                        try:
                            h, m, s = match.groups()
                            cur = int(h) * 3600 + int(m) * 60 + float(s)
                            dur = self.app.queue_data[row]["end"] - self.app.queue_data[row]["start"]
                            if dur > 0:
                                pct = min(100, int((cur / dur) * 100))
                                elapsed_time = time.time() - self.start_time
                                rem = max(0, int(((elapsed_time / pct) * 100) - elapsed_time)) if pct > 0 else 0
                                self.progress_updated.emit(row, pct, rem)
                        except Exception as e:
                            logging.debug(f"Ошибка парсинга прогресса: {e}")
                    stderr_buffer = ""

            exit_code = self.ffmpeg_proc.wait()
            logging.info(f"FFmpeg завершился с кодом: {exit_code}")
            if exit_code != 0:
                logging.error(f"FFmpeg вернул код {exit_code}")
                logging.error(f"Последние 20 строк stderr:")
                for line in stderr_lines[-20:]:
                    logging.error(f"  | {line}")
            self.encode_finished.emit(row, exit_code)
        except Exception as e:
            logging.error(f"Ошибка чтения прогресса: {e}")
            logging.error(traceback.format_exc())
            self.encode_failed.emit(row)

    def stop_process(self, stop_whole_queue=True):
        """
        ИСПРАВЛЕНИЕ: остановка теперь действует на ВСЮ очередь сразу.
        1) Убивается текущий процесс FFmpeg;
        2) Выставляется флаг stop_requested — encode_next больше не запустит следующее видео;
        3) Все неотработанные строки очереди помечаются "Остановлено".
        Файл удаляется ТОЛЬКО если процесс был принудительно убит.
        """
        row = self.app.current_encoding_row
        self.app.current_encoding_row = -1

        if stop_whole_queue:
            self.stop_requested = True

        # Флаг: действительно ли мы принудительно убили процесс?
        process_was_killed = False

        if self.ffmpeg_proc:
            try:
                if self.ffmpeg_proc.poll() is None:
                    logging.info("Принудительная остановка процесса FFmpeg.")
                    if sys.platform == "win32":
                        try:
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.ffmpeg_proc.pid)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          creationflags=CREATION_FLAGS)
                        except Exception as e:
                            logging.error(f"Ошибка taskkill: {e}")
                            self.ffmpeg_proc.kill()
                    else:
                        self.ffmpeg_proc.kill()
                    self.ffmpeg_proc.wait(timeout=5)
                    process_was_killed = True
            except Exception as e:
                logging.error(f"Ошибка остановки: {e}")

        if row != -1 and row in self.app.queue_data:
            status_item = self.app.table.item(row, 4)
            if status_item:
                status_item.setText("Остановлено" if stop_whole_queue else "В очереди")
            pbar = self.app.table.cellWidget(row, 3)
            if isinstance(pbar, QProgressBar):
                pbar.setValue(0)

        # ИСПРАВЛЕНИЕ: помечаем ВСЮ оставшуюся очередь как остановленную,
        # чтобы ни одно следующее видео не запускалось
        if stop_whole_queue:
            for r in range(self.app.table.rowCount()):
                s_item = self.app.table.item(r, 4)
                if s_item:
                    txt = s_item.text()
                    if txt in ("Сжатие...", "В очереди", "Кроп задан") or "Разрешение:" in txt:
                        s_item.setText("Остановлено")
                        pb = self.app.table.cellWidget(r, 3)
                        if isinstance(pb, QProgressBar):
                            pb.setValue(0)

        out_file = getattr(self.app, 'current_output_file', '')
        # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: удаляем файл ТОЛЬКО если процесс был убит
        if process_was_killed and out_file and os.path.exists(out_file):
            for attempt in range(5):
                try:
                    os.remove(out_file)
                    logging.info(f"Недоделанный файл удален: {out_file}")
                    break
                except PermissionError:
                    time.sleep(0.5)
                except Exception as e:
                    logging.error(f"Не удалось удалить файл {out_file}: {e}")
                    break

        self.app.current_output_file = ""


# ============================================================================
# ГЛАВНОЕ ОКНО
# ============================================================================
class GpuSqueezerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CachyOS GPU Squeezer")
        self.resize(1300, 880)
        icon_path = os.path.join(EXE_DIR, "gpu-squeezer.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.queue_data = {}
        self.current_encoding_row = -1
        self.last_output_dir = os.path.expanduser("~")
        self.custom_save_dir = ""
        self.current_output_file = ""
        self.settings = QSettings("CachyOS", "CachyOS GPU Squeezer")

        self.core = SqueezerCore(self)
        self.core.progress_updated.connect(self.update_ui_progress)
        self.core.encode_finished.connect(self.on_encode_finished)
        self.core.encode_failed.connect(self.on_encode_failed)

        self.setAcceptDrops(True)
        self.setStyleSheet(CACHYOS_STYLESHEET)
        self._setup_ui()

        self.lbl_calculated_info = QLabel("Расчетный битрейт: --")
        self.statusBar().addPermanentWidget(self.lbl_calculated_info)

        self.load_saved_settings()

    def _setup_ui(self):
        widget = QWidget()
        layout = QHBoxLayout(widget)

        left = QVBoxLayout()
        btns = QHBoxLayout()
        self.btn_add = QPushButton("📁 Добавить файлы")
        self.btn_add.clicked.connect(self.add_files)
        self.btn_remove = QPushButton(" Удалить из очереди")
        self.btn_remove.setStyleSheet("border-color: #e74c3c;")
        self.btn_remove.clicked.connect(self.remove_selected_file)
        btns.addWidget(self.btn_add)
        btns.addWidget(self.btn_remove)
        left.addLayout(btns)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Имя", "Длина (сек)", "Битрейт", "Прогресс", "Статус"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self.on_row_selected)
        left.addWidget(self.table)

        queue_ctrls = QHBoxLayout()
        self.btn_start_queue = QPushButton("▶ Запустить сжатие")
        self.btn_start_queue.setStyleSheet("background-color: #00aa7f; color: #ffffff; font-weight: bold; font-size: 14px; padding: 8px;")
        self.btn_start_queue.clicked.connect(self.start_queue_processing)
        self.btn_stop_queue = QPushButton("🛑 Остановить")
        self.btn_stop_queue.setStyleSheet("border-color: #e74c3c; font-size: 14px; padding: 8px;")
        self.btn_stop_queue.clicked.connect(self.stop_queue)
        queue_ctrls.addWidget(self.btn_start_queue, stretch=2)
        queue_ctrls.addWidget(self.btn_stop_queue, stretch=1)
        left.addLayout(queue_ctrls)

        dest = QHBoxLayout()
        dest.addWidget(QLabel("Папка сохранения:"))
        self.txt_dest_dir = QLineEdit("По умолчанию (рядом с оригиналом)")
        self.txt_dest_dir.setReadOnly(True)
        dest.addWidget(self.txt_dest_dir)
        self.btn_browse_dest = QPushButton("Выбрать...")
        self.btn_browse_dest.clicked.connect(self.choose_destination_directory)
        dest.addWidget(self.btn_browse_dest)
        left.addLayout(dest)

        layout.addLayout(left, stretch=4)

        right = QVBoxLayout()
        self.player = SqueezerPlayer(range_callback=self.on_slider_range_moved)
        right.addWidget(self.player, stretch=3)

        top_ctrl_layout = QHBoxLayout()
        top_ctrl_layout.addWidget(QLabel("🔊 Громкость:"))
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(70)
        self.vol_slider.setFixedWidth(120)
        self.vol_slider.valueChanged.connect(self.change_volume)
        top_ctrl_layout.addWidget(self.vol_slider)
        self.btn_crop = QPushButton("️ Кадрирование (Crop)")
        self.btn_crop.setStyleSheet("border-color: #00ffbc; font-weight: bold;")
        self.btn_crop.clicked.connect(self.open_crop_dialog)
        top_ctrl_layout.addWidget(self.btn_crop)
        top_ctrl_layout.addStretch()
        right.addLayout(top_ctrl_layout)

        p_ctrls = QHBoxLayout()
        self.btn_play = QPushButton("▶ / ⏸")
        self.btn_set_start = QPushButton("⏱ Старт")
        self.btn_set_end = QPushButton("⏱ Конец")
        self.btn_play.clicked.connect(self.player.toggle_play)
        self.btn_set_start.clicked.connect(self.set_start_marker)
        self.btn_set_end.clicked.connect(self.set_end_marker)
        p_ctrls.addWidget(self.btn_play)
        p_ctrls.addWidget(self.btn_set_start)
        p_ctrls.addWidget(self.btn_set_end)
        right.addLayout(p_ctrls)

        self.lbl_markers = QLabel("Старт: 0.00 сек | Конец: 0.00 сек")
        right.addWidget(self.lbl_markers)

        settings = QWidget()
        settings.setStyleSheet("background-color: #21262e; border-radius: 6px; padding: 5px;")
        s_vbox = QVBoxLayout(settings)

        gb_lay = QHBoxLayout()
        gb_lay.addWidget(QLabel("Глобальный битрейт (кб/с):"))
        self.txt_global_bitrate = QLineEdit("6500")
        self.txt_global_bitrate.textChanged.connect(self.recalculate_current_row)
        gb_lay.addWidget(self.txt_global_bitrate)
        s_vbox.addLayout(gb_lay)

        res_lay = QHBoxLayout()
        res_lay.addWidget(QLabel("Разрешение выхода:"))
        self.combo_res = QComboBox()
        self.combo_res.addItems(["Оригинал", "1920x1080", "1280x720", "3840x2160", "640x480"])
        self.combo_res.currentIndexChanged.connect(self.change_target_resolution)
        res_lay.addWidget(self.combo_res)
        self.btn_apply_res_all = QPushButton("🔄 Применить ко всем")
        self.btn_apply_res_all.setStyleSheet("background-color: #00aa7f; color: white; font-weight: bold; padding: 4px 10px;")
        self.btn_apply_res_all.clicked.connect(self.apply_resolution_to_all)
        res_lay.addWidget(self.btn_apply_res_all)
        self.lbl_source_res = QLabel("Исходное: --")
        res_lay.addWidget(self.lbl_source_res)
        res_lay.addStretch()
        s_vbox.addLayout(res_lay)

        limit_layout = QHBoxLayout()
        self.chk_strict_limit = QCheckBox("Ужимать строго до лимита:")
        self.chk_strict_limit.setChecked(True)
        self.chk_strict_limit.stateChanged.connect(self.toggle_limit_input)
        self.txt_target_size = QLineEdit("2.00")
        self.txt_target_size.setFixedWidth(60)
        self.txt_target_size.textChanged.connect(self.recalculate_current_row)
        limit_layout.addWidget(self.chk_strict_limit)
        limit_layout.addWidget(self.txt_target_size)
        limit_layout.addWidget(QLabel("ГБ"))
        limit_layout.addStretch()
        s_vbox.addLayout(limit_layout)

        right.addWidget(settings)
        layout.addLayout(right, stretch=3)

        self.setCentralWidget(widget)

    def stop_queue(self):
        logging.info("Пользователь нажал Остановить (останавливается ВСЯ очередь)")
        # ИСПРАВЛЕНИЕ: stop_process теперь сам убивает текущий процесс,
        # выставляет флаг остановки и помечает все оставшиеся строки "Остановлено"
        self.core.stop_process(stop_whole_queue=True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        file_paths = [url.toLocalFile() for url in event.mimeData().urls()
                     if os.path.isfile(url.toLocalFile()) and url.toLocalFile().lower().endswith(('.mp4', '.mkv', '.avi', '.mov'))]
        if file_paths:
            self.process_incoming_files(file_paths)

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Добавить видео", self.last_output_dir,
                                                "Видео (*.mp4 *.mkv *.avi *.mov)")
        if files:
            self.process_incoming_files(files)

    def process_incoming_files(self, paths):
        was_empty = (self.table.rowCount() == 0)
        self.table.blockSignals(True)
        for path in paths:
            if any(d["path"] == path for d in self.queue_data.values()):
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            duration = self.core.get_video_duration(path)
            self.queue_data[row] = {
                "path": path, "start": 0.0, "end": duration, "duration": duration,
                "target_res": "Оригинал", "custom_enabled": False, "custom_val": "6500",
                "bitrate": 6500, "crop_area": ""
            }
            self.table.setItem(row, 0, QTableWidgetItem(os.path.basename(path)))
            self.table.setItem(row, 1, QTableWidgetItem(f"{duration:.2f}"))
            pbar = QProgressBar()
            pbar.setValue(0)
            self.table.setCellWidget(row, 3, pbar)
            self.table.setItem(row, 4, QTableWidgetItem("В очереди"))
        self.table.blockSignals(False)

        for row in range(self.table.rowCount()):
            self._recalculate_row_safely(row)

        if was_empty and self.table.rowCount() > 0:
            self.table.setCurrentCell(0, 0)

    def _recalculate_row_safely(self, row):
        if row not in self.queue_data:
            return
        strict = self.chk_strict_limit.isChecked()
        try:
            size_gb = float(self.txt_target_size.text())
        except ValueError:
            size_gb = 2.00
        global_val = self.txt_global_bitrate.text()
        result = self.core.calculate_bitrate(row, strict, size_gb, global_val)
        if result:
            bitrate_val, status_text = result
            self.queue_data[row]["bitrate"] = bitrate_val
            self.table.setItem(row, 2, QTableWidgetItem(status_text))
            if row == self.table.currentRow():
                self.lbl_calculated_info.setText(f"Расчетный битрейт: {bitrate_val} кб/с")

    def update_ui_progress(self, row, percentage, remaining_seconds):
        pbar = self.table.cellWidget(row, 3)
        if isinstance(pbar, QProgressBar):
            pbar.setValue(percentage)
        self.table.setItem(row, 4, QTableWidgetItem(f"{remaining_seconds}с"))

    def on_encode_finished(self, row, exit_code):
        logging.info(f"Кодирование строки {row} завершено с кодом {exit_code}")
        self.current_encoding_row = -1

        if self.core.stop_requested:
            # Пользователь остановил ВСЮ очередь — ничего не перезапускаем,
            # статусы уже расставлены в stop_process
            return

        if exit_code == 0:
            pbar = self.table.cellWidget(row, 3)
            if isinstance(pbar, QProgressBar):
                pbar.setValue(100)
            self.table.setItem(row, 4, QTableWidgetItem("✓ Готово"))
        else:
            self.table.setItem(row, 4, QTableWidgetItem("✗ Ошибка"))
            # При ошибке очищаем путь, чтобы stop_process не удалил что-то лишнее
            self.current_output_file = ""

        self.encode_next()

    def on_encode_failed(self, row):
        logging.error(f"Сбой строки {row}")
        self.current_encoding_row = -1
        self.current_output_file = ""
        if self.core.stop_requested:
            return
        self.table.setItem(row, 4, QTableWidgetItem(" Сбой"))
        self.encode_next()

    def recalculate_current_row(self):
        self._recalculate_row_safely(self.table.currentRow())

    def change_target_resolution(self):
        row = self.table.currentRow()
        if row != -1 and row in self.queue_data:
            selected_res = self.combo_res.currentText()
            self.queue_data[row]["target_res"] = selected_res
            self._recalculate_row_safely(row)
            current_status = self.table.item(row, 4).text() if self.table.item(row, 4) else "В очереди"
            if current_status not in ["Сжатие...", "✓ Готово", " Ошибка", "✗ Сбой"]:
                self.table.setItem(row, 4, QTableWidgetItem(f"Разрешение: {selected_res}"))

    def apply_resolution_to_all(self):
        selected_res = self.combo_res.currentText()
        for row in range(self.table.rowCount()):
            if row in self.queue_data:
                self.queue_data[row]["target_res"] = selected_res
                current_status = self.table.item(row, 4).text() if self.table.item(row, 4) else "В очереди"
                if current_status not in ["Сжатие...", "✓ Готово", "✗ Ошибка", "✗ Сбой"]:
                    self.table.setItem(row, 4, QTableWidgetItem(f"Разрешение: {selected_res}"))
        self.recalculate_current_row()

    def toggle_limit_input(self, state):
        self.txt_target_size.setEnabled(self.chk_strict_limit.isChecked())
        self.recalculate_current_row()

    def remove_selected_file(self):
        row = self.table.currentRow()
        if row == -1:
            return
        self.table.blockSignals(True)
        self.table.removeRow(row)
        self.table.blockSignals(False)
        new_queue_data = {}
        new_idx = 0
        for old_idx, data in sorted(self.queue_data.items()):
            if old_idx == row:
                continue
            new_queue_data[new_idx] = data
            new_idx += 1
        self.queue_data = new_queue_data

        if self.table.rowCount() == 0:
            try:
                self.player.media_player.stop()
                self.player.media_player.setSource(QUrl())
                self.player.current_frame = QImage()
                self.player.video_canvas.update()
            except Exception as e:
                logging.error(f"Ошибка очистки плеера: {e}")
            self.lbl_markers.setText("Старт: 0.00 сек | Конец: 0.00 сек")
            self.lbl_source_res.setText("Исходное: --")
            self.lbl_calculated_info.setText("Расчетный битрейт: --")
        else:
            next_row = max(0, row - 1)
            self.table.setCurrentCell(next_row, 0)
            self.on_row_selected()

    def choose_destination_directory(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Выберите папку сохранения",
                                                    self.last_output_dir)
        if dir_path:
            self.custom_save_dir = dir_path
            self.txt_dest_dir.setText(dir_path)
            self.settings.setValue("custom_save_dir", dir_path)

    def on_row_selected(self):
        row = self.table.currentRow()
        if row == -1 or row not in self.queue_data:
            return
        data = self.queue_data[row]
        logging.info(f"Выбрана строка {row}: {data['path']}")
        
        # АСИНХРОННАЯ загрузка — не блокирует GUI
        self.player.load_video(data["path"])
        
        # Используем КЕШ — повторный вызов мгновенный (<1 мс)
        self.lbl_source_res.setText(f"Исходное: {self.core.get_video_resolution(data['path'])}")
        
        self.player.slider.set_values(int(data["start"] * 1000), int(data["end"] * 1000),
                                      int(self.player.media_player.position()))
        self.lbl_markers.setText(f"Старт: {data['start']:.2f} сек | Конец: {data['end']:.2f} сек")
        if data.get("crop_area"):
            self.player.update_crop_mask(data["crop_area"])
        else:
            self.player.update_crop_mask("")
        self.recalculate_current_row()

    def on_slider_range_moved(self, start_ms, end_ms):
        row = self.table.currentRow()
        if row != -1 and row in self.queue_data:
            self.queue_data[row]["start"] = start_ms / 1000.0
            self.queue_data[row]["end"] = end_ms / 1000.0
            self.lbl_markers.setText(f"Старт: {self.queue_data[row]['start']:.2f} сек | "
                                     f"Конец: {self.queue_data[row]['end']:.2f} сек")
            self.recalculate_current_row()

    def change_volume(self, value):
        self.player.audio_output.setVolume(value / 100.0)

    def open_crop_dialog(self):
        row = self.table.currentRow()
        if row == -1 or row not in self.queue_data:
            return
        dialog = CropDialog(self.queue_data[row]["path"], self.player.media_player.position(), self)
        if dialog.exec() == CropDialog.DialogCode.Accepted and dialog.crop_area_str:
            self.queue_data[row]["crop_area"] = dialog.crop_area_str
            self.table.setItem(row, 4, QTableWidgetItem("Кроп задан"))
            self.player.update_crop_mask(dialog.crop_area_str)

    def set_start_marker(self):
        row = self.table.currentRow()
        if row != -1:
            curr_ms = self.player.media_player.position()
            self.player.slider.set_values(curr_ms, self.player.slider.end_pos, curr_ms)
            self.on_slider_range_moved(curr_ms, self.player.slider.end_pos)

    def set_end_marker(self):
        row = self.table.currentRow()
        if row != -1:
            curr_ms = self.player.media_player.position()
            self.player.slider.set_values(self.player.slider.start_pos, curr_ms, curr_ms)
            self.on_slider_range_moved(self.player.slider.start_pos, curr_ms)

    def encode_next(self):
        if self.core.stop_requested:
            return
        if self.current_encoding_row != -1:
            return
        for row in range(self.table.rowCount()):
            status_item = self.table.item(row, 4)
            if status_item and (status_item.text() in ["В очереди", "Кроп задан"] or
                               "Разрешение:" in status_item.text()):
                self.current_encoding_row = row
                status_item.setText("Сжатие...")
                if not self.core.run_ffmpeg_process(row, self.custom_save_dir):
                    self.encode_next()
                return
        self.current_encoding_row = -1

    def start_queue_processing(self):
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "Пустая очередь", "Добавьте видеофайлы перед запуском.")
            return
        has_pending = False
        for row in range(self.table.rowCount()):
            status_item = self.table.item(row, 4)
            if status_item and (status_item.text() in ["В очереди", "Кроп задан"] or
                               "Разрешение:" in status_item.text()):
                has_pending = True
                break
        if not has_pending:
            QMessageBox.information(self, "Нечего сжимать", "Все файлы уже обработаны.")
            return
        # Пользователь снова нажал "Запустить" — снимаем флаг остановки очереди
        self.core.stop_requested = False
        self.encode_next()

    def load_saved_settings(self):
        self.last_output_dir = self.settings.value("last_output_dir", os.path.expanduser("~"))
        self.txt_global_bitrate.setText(self.settings.value("global_bitrate", "6500"))
        # ЗАГРУЗКА целевого размера из настроек (по умолчанию 2.00 ГБ)
        saved_target_size = self.settings.value("target_size_gb", "2.00")
        self.txt_target_size.setText(saved_target_size)
        saved_dir = self.settings.value("custom_save_dir", "")
        if saved_dir and os.path.exists(saved_dir):
            self.custom_save_dir = saved_dir
            self.txt_dest_dir.setText(saved_dir)
        else:
            self.txt_dest_dir.setText("По умолчанию (рядом с оригиналом)")

    def closeEvent(self, event):
        """Гарантированно останавливаем процесс при закрытии"""
        logging.info("Закрытие приложения...")
        self.settings.setValue("last_output_dir", self.last_output_dir)
        self.settings.setValue("global_bitrate", self.txt_global_bitrate.text())
        # СОХРАНЕНИЕ целевого размера в настройках
        self.settings.setValue("target_size_gb", self.txt_target_size.text())
        self.settings.setValue("custom_save_dir", self.custom_save_dir)
        
        # Принудительно останавливаем процесс (файл-заглушку для update() не создаём)
        try:
            self.core.stop_process(stop_whole_queue=False)
        except Exception as e:
            logging.error(f"Ошибка остановки процесса при закрытии: {e}")
        
        event.accept()


# ============================================================================
# ОКНО ЗАГРУЗКИ КОМПОНЕНТОВ + СТАРТ БЕЗ КОНСОЛИ
# ============================================================================
class ComponentLoaderDialog(QDialog):
    """Модальное окно 'единым окном' — показывает прогресс скачивания компонентов."""
    progress_updated = pyqtSignal(int)
    status_changed = pyqtSignal(str)
    finished_loading = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CachyOS GPU Squeezer — подготовка")
        self.setFixedSize(460, 170)
        # Убираем крестик — диалог модальный и закрывается сам по завершении загрузки
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
                            & ~Qt.WindowType.WindowCloseButtonHint)
        self.problems = []

        layout = QVBoxLayout(self)
        self.lbl_status = QLabel("Проверка недостающих компонентов (ffmpeg/ffprobe)...")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self._result = None
        self._thread = None

        self.progress_updated.connect(self.progress.setValue)
        self.status_changed.connect(self.lbl_status.setText)
        self.finished_loading.connect(self._on_finished)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(150)
        self._poll_timer.timeout.connect(self._check_done)

    def start_loading(self):
        # Фоновый поток скачивает компоненты; сигналы Qt гарантированно
        # доставляются в главный поток, GUI не блокируется
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        self._poll_timer.start()

    def _worker(self):
        try:
            problems = ensure_components(
                progress_cb=lambda p: self.progress_updated.emit(p),
                status_cb=lambda t: self.status_changed.emit(t))
        except Exception as e:
            logging.error(f"Ошибка загрузчика компонентов: {e}")
            problems = [str(e)]
        self.finished_loading.emit(problems)

    def _on_finished(self, problems):
        self._result = problems

    def _check_done(self):
        if self._result is not None:
            self._poll_timer.stop()
            self.accept()


def _apply_runtime_paths():
    """После возможного скачивания пересчитываем пути к ffmpeg/ffprobe."""
    global FFMPEG_LONG, FFPROBE_LONG, FFMPEG_EXE, FFPROBE_EXE
    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    probe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    local_ff = os.path.join(EXE_DIR, exe_name)
    local_fp = os.path.join(EXE_DIR, probe_name)
    FFMPEG_LONG = local_ff if os.path.exists(local_ff) else "ffmpeg"
    FFPROBE_LONG = local_fp if os.path.exists(local_fp) else "ffprobe"
    FFMPEG_EXE = get_short_path(FFMPEG_LONG)
    FFPROBE_EXE = get_short_path(FFPROBE_LONG)
    logging.info(f"Обновлённые пути: FFMPEG_EXE={FFMPEG_EXE}, FFPROBE_EXE={FFPROBE_EXE}")


def _hide_console_windows():
    """Гарантированное скрытие консоли на Windows (если она всё ещё есть).

    Вызывается как страховка в тех редких случаях, когда перезапуск через
    pythonw.exe невозможен (например, в окружении нет pythonw.exe):
    окно консоли прячется через ShowWindow(SW_HIDE), а затем процесс
    отсоединяется от неё через FreeConsole.
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            SW_HIDE = 0
            user32.ShowWindow(hwnd, SW_HIDE)
            kernel32.FreeConsole()
    except Exception:
        pass


def main():
    """
    Старт приложения без консоли единым окном:
    - консольное окно закрывается/прячется в самом начале файла (перезапуск
      через pythonw.exe до импорта PyQt + гарантированное скрытие через
      Windows API как страховка);
    - для собранного EXE используется PyInstaller с флагом --noconsole
      (--windowed), тогда чёрного окна нет по построению;
    - при старте автоматически скачиваются недостающие компоненты
      (ffmpeg/ffprobe).
    """
    # Страховка: если по какой-то причине мы всё ещё запущены с консолью
    # (pythonw.exe недоступен и т.п.) — скрываем её немедленно.
    _hide_console_windows()

    app = QApplication(sys.argv)

    # --- Автопроверка/скачивание недостающих компонентов при старте ---
    loader = ComponentLoaderDialog()
    loader.start_loading()
    loader.exec()

    _apply_runtime_paths()
    if loader._result:
        QMessageBox.warning(None, "Компоненты не установлены",
                            "\n".join(loader._result) +
                            "\n\nПриложение запустится, но конвертация будет недоступна, "
                            "пока ffmpeg/ffprobe не появятся в папке программы или в PATH.")

    window = GpuSqueezerApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()