from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import font_loader
from engine import (
    EncodeSettings, Job, MontageError, benchmark_encode, estimated_bytes,
    format_time, parse_time, probe, render, validate,
)

BG = "#0d1117"
CARD = "#161b22"
INPUT = "#21262d"
TEXT = "#f0f6fc"
SOFT = "#8b949e"
GREEN = "#238636"
GREEN_HOVER = "#2ea043"
BLUE = "#1f6feb"
RED = "#da3633"

APP_FONT_FAMILY = font_loader.load_bundled_fonts()
ctk.ThemeManager.theme["CTkFont"]["family"] = APP_FONT_FAMILY
CURRENT_LANGUAGE = "tr"


def T(tr: str, en: str) -> str:
    return en if CURRENT_LANGUAGE == "en" else tr


EN_TO_TR = {
    "Horizontal": "Yatay", "Vertical": "Dikey",
    "Source Values": "Kaynak Değerleri", "Custom": "Özel",
    **{f"Quick Setting {i}": f"Hızlı Ayar {i}" for i in range(1, 6)},
}
PROFILE_EN = {
    "480p Dengeli": "480p Balanced",
    "720p Dengeli": "720p Balanced",
    "1080p Dengeli": "1080p Balanced",
    "1080p Yüksek": "1080p High",
    "1440p": "1440p",
    "4K": "4K",
}
EN_TO_TR.update({en: tr for tr, en in PROFILE_EN.items()})


def canonical(value: str) -> str:
    return EN_TO_TR.get(value, value)


def error_text(message: str) -> str:
    if CURRENT_LANGUAGE != "en":
        return message
    replacements = {
        "FFmpeg bulunamadı. Uygulama eksik veya bozuk kurulmuş.":
            "FFmpeg was not found. The application is incomplete or corrupted.",
        "Dosya bulunamadı:": "File not found:",
        "Geçerli bir video okunamadı:": "A valid video could not be read:",
        "Geçersiz süre:": "Invalid time:",
        "biçimini kullanın.": "format.",
        "Çıktı klasörü bulunamadı:": "Output folder not found:",
        "Çıktı dosyası ana videonun üzerine yazılamaz.":
            "The output cannot overwrite the main video.",
        "Çıktı dosyası .mp4 olmalıdır.": "The output file must be .mp4.",
        "Kırpma videonun tamamını siliyor.": "The trim removes the entire video.",
        "İç kırpma aralığı kalan video süresinin içinde olmalıdır.":
            "The internal trim range must be within the remaining video.",
        "Görsel bulunamadı:": "Image not found:",
        "Encoding ayarları oluşturulamadı.": "Encoding settings could not be created.",
        "Encoding değerleri sıfırdan büyük olmalıdır.":
            "Encoding values must be greater than zero.",
        "Çözünürlük değerleri çift sayı olmalıdır.":
            "Resolution values must be even numbers.",
        "PC gücü testi için video en az 1 saniye olmalıdır.":
            "The video must be at least 1 second for the performance test.",
        "PC gücü ölçülemedi.": "PC performance could not be measured.",
        "İşlem kullanıcı tarafından iptal edildi.": "Processing was cancelled by the user.",
        "FFmpeg işlemi tamamlayamadı.": "FFmpeg could not complete the operation.",
        "İşlem bitti ancak geçerli çıktı dosyası oluşmadı.":
            "Processing ended but no valid output was created.",
        "Encoding alanlarından biri geçersiz.": "One of the encoding fields is invalid.",
        "Önce ana MP4 dosyasını seçin.": "Select the main MP4 file first.",
    }
    result = message
    for tr, en in replacements.items():
        result = result.replace(tr, en)
    return result


def settings_file() -> Path:
    root = Path(os.environ.get("APPDATA", Path.home()))
    return root / "MediaEditor" / "settings.json"


def load_app_settings() -> dict:
    try:
        data = json.loads(settings_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_app_settings(data: dict) -> None:
    target = settings_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_output_dir() -> Path:
    fallback = Path.home() / "Videos"
    if not fallback.is_dir():
        fallback = Path.home() / "Desktop"
    try:
        data = load_app_settings()
        saved = Path(data["output_dir"])
        if saved.is_dir():
            return saved
    except (OSError, KeyError, ValueError, TypeError):
        pass
    return fallback


def save_output_dir(path: Path) -> None:
    data = load_app_settings()
    data["output_dir"] = str(path)
    save_app_settings(data)


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return ""


def estimated_process_seconds(duration: float, settings: EncodeSettings) -> float:
    """Veryfast CPU encoding için temkinli bir ilk tahmin.

    Donanım ölçümü bulunmadığı ilk çalıştırmada çözünürlük, FPS ve codec iş
    yükünden hesaplanır. Bu değer kesin süre değil, kullanıcıya ölçek verir.
    """
    return max(2.0, duration * heuristic_process_factor(settings))


def heuristic_process_factor(settings: EncodeSettings) -> float:
    pixel_load = (settings.width * settings.height * settings.fps) / (
        1920 * 1080 * 30)
    codec_load = 1.8 if settings.codec.lower() == "h265" else 1.0
    preset_load = {
        "ultrafast": 0.55, "superfast": 0.72, "veryfast": 1.0,
        "faster": 1.2, "fast": 1.45, "medium": 2.0, "slow": 2.8,
    }.get(settings.preset, 1.0)
    return max(0.12, 0.42 * pixel_load * codec_load * preset_load)


class FileRows(ctk.CTkFrame):
    def __init__(self, master, title: str, filetypes, timed=False):
        super().__init__(master, fg_color="transparent")
        self.filetypes = filetypes
        self.timed = timed
        self.rows: list[dict] = []
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=title, text_color=TEXT,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkButton(head, text=T("+ Ekle", "+ Add"), width=72, height=28, fg_color=BLUE,
                      command=self.add).pack(side="right")
        self.body = ctk.CTkFrame(self, fg_color=INPUT, corner_radius=8)
        self.body.pack(fill="x", pady=(6, 12))
        self.redraw()

    def add(self):
        path = filedialog.askopenfilename(filetypes=self.filetypes)
        if path:
            self.rows.append({"path": Path(path), "time": "00:00:00"})
            self.redraw()

    def redraw(self):
        for widget in self.body.winfo_children():
            widget.destroy()
        if not self.rows:
            ctk.CTkLabel(self.body, text=T("Dosya eklenmedi", "No file added"),
                         text_color=SOFT).pack(
                anchor="w", padx=10, pady=10)
            return
        for i, row in enumerate(self.rows):
            line = ctk.CTkFrame(self.body, fg_color="transparent")
            line.pack(fill="x", padx=6, pady=4)
            if self.timed:
                entry = ctk.CTkEntry(line, width=92)
                entry.insert(0, row["time"])
                entry.pack(side="left", padx=(0, 6))
                entry.bind("<FocusOut>", lambda _e, r=row, w=entry: r.update(time=w.get()))
            ctk.CTkLabel(line, text=row["path"].name, anchor="w",
                         text_color=TEXT).pack(side="left", fill="x", expand=True)
            for text, action in (
                ("↑", lambda n=i: self.move(n, -1)),
                ("↓", lambda n=i: self.move(n, 1)),
                ("×", lambda n=i: self.remove(n)),
            ):
                ctk.CTkButton(line, text=text, width=28, height=26,
                              fg_color=RED if text == "×" else INPUT,
                              command=action).pack(side="left", padx=2)

    def move(self, index, direction):
        new = index + direction
        if 0 <= new < len(self.rows):
            self.rows[index], self.rows[new] = self.rows[new], self.rows[index]
            self.redraw()

    def remove(self, index):
        self.rows.pop(index)
        self.redraw()

    def paths(self):
        return [row["path"] for row in self.rows]

    def timed_paths(self):
        return [(parse_time(row["time"]), row["path"]) for row in self.rows]


class OverlayRows(FileRows):
    def __init__(self, master):
        super().__init__(master, T("Videonun Üzerine Görsel Ekle", "Overlay Image"), [
            (T("Görseller", "Images"), "*.png *.jpg *.jpeg *.webp *.bmp")], timed=True)

    def add(self):
        path = filedialog.askopenfilename(filetypes=self.filetypes)
        if path:
            self.rows.append({"path": Path(path), "time": "00:00:00",
                              "duration": "00:00:03"})
            self.redraw()

    def redraw(self):
        if not hasattr(self, "body"):
            return
        for widget in self.body.winfo_children():
            widget.destroy()
        if not self.rows:
            ctk.CTkLabel(self.body, text=T("Görsel eklenmedi", "No image added"),
                         text_color=SOFT).pack(
                anchor="w", padx=10, pady=10)
            return
        for i, row in enumerate(self.rows):
            line = ctk.CTkFrame(self.body, fg_color="transparent")
            line.pack(fill="x", padx=6, pady=4)
            for key, label_text in (
                    ("time", T("Ekrana geleceği zaman:", "Display time:")),
                    ("duration", T("Kalma süresi:", "Duration:"))):
                ctk.CTkLabel(
                    line, text=label_text, text_color=SOFT,
                    font=ctk.CTkFont(size=11)).pack(side="left", padx=(4, 5))
                entry = ctk.CTkEntry(line, width=88)
                entry.insert(0, row[key])
                entry.pack(side="left", padx=(0, 5))
                entry.bind("<FocusOut>", lambda _e, r=row, k=key, w=entry:
                           r.update({k: w.get()}))
            ctk.CTkLabel(line, text=row["path"].name, anchor="w").pack(
                side="left", fill="x", expand=True)
            ctk.CTkButton(line, text="×", width=28, height=26, fg_color=RED,
                          command=lambda n=i: self.remove(n)).pack(side="right")

    def overlays(self):
        return [(parse_time(r["time"]), parse_time(r["duration"]), r["path"])
                for r in self.rows]


class MediaEditorApp(ctk.CTk):
    PROFILES = {
        "Kaynak Değerleri": None,
        "480p Dengeli": ("h264", "854x480", "30", "1", "aac", "128"),
        "720p Dengeli": ("h264", "1280x720", "30", "5", "aac", "192"),
        "1080p Dengeli": ("h264", "1920x1080", "30", "8", "aac", "192"),
        "1080p Yüksek": ("h264", "1920x1080", "60", "10", "aac", "256"),
        "1440p": ("h265", "2560x1440", "60", "16", "aac", "256"),
        "4K": ("h265", "3840x2160", "60", "35", "aac", "320"),
    }

    def __init__(self):
        global CURRENT_LANGUAGE
        CURRENT_LANGUAGE = load_app_settings().get("language", "tr")
        super().__init__()
        self.title("Media Editor")
        self.geometry("1120x760")
        self.minsize(980, 680)
        self.configure(fg_color=BG)
        self.source: Path | None = None
        self.output_dir: Path = load_output_dir()
        self.info = None
        saved_profiles = load_app_settings().get("quick_profiles", {})
        if "Yatay" in saved_profiles or "Dikey" in saved_profiles:
            self.quick_profiles = saved_profiles
        else:
            self.quick_profiles = {"Yatay": saved_profiles, "Dikey": {}}
        self.benchmark_factors = {}
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self._build()
        self.after(100, self._poll)

    def _build(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=22, pady=(18, 12))
        ctk.CTkLabel(header, text="MEDIA EDITOR", text_color=TEXT,
                     font=ctk.CTkFont(size=24, weight="bold")).pack(
            side="left", fill="x", expand=True)
        self.language_switch = ctk.CTkSegmentedButton(
            header, values=["TR", "EN"], width=94, command=self.switch_language)
        self.language_switch.set(CURRENT_LANGUAGE.upper())
        self.language_switch.pack(side="right")
        source_card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        source_card.pack(fill="x", padx=22)
        ctk.CTkButton(source_card, text=T("Asıl Video", "Main Video"), fg_color=BLUE,
                      command=self.choose_source).pack(side="left", padx=12, pady=12)
        self.output_button = ctk.CTkButton(
            source_card, text=self.output_button_text(), fg_color=BLUE,
            command=self.choose_output_dir)
        self.output_button.pack(side="right", padx=12, pady=12)
        self.info_label = ctk.CTkLabel(source_card, text="", text_color=SOFT)
        self.info_label.pack(side="right", padx=4)
        self.source_label = ctk.CTkLabel(
            source_card, text=T("Henüz ana video seçilmedi", "No main video selected"),
            text_color=SOFT, anchor="w")
        self.source_label.pack(side="left", fill="x", expand=True)

        nav = ctk.CTkFrame(self, fg_color=INPUT, corner_radius=10)
        nav.pack(fill="x", padx=22, pady=12)
        self.pages = {}
        self.page_host = ctk.CTkFrame(self, fg_color="transparent")
        self.page_host.pack(fill="both", expand=True, padx=22)
        self.nav_buttons = {}
        for key, name, method in (
                ("trim", T("Kırpma", "Trim"), self.show_trim),
                ("add", T("Ekleme", "Add"), self.show_add),
                ("encode", "Encoding", self.show_encode)):
            button = ctk.CTkButton(
                nav, text=name, fg_color=CARD, hover_color=BLUE,
                height=40,
                border_width=1, border_color="#30363d",
                font=ctk.CTkFont(
                    family=APP_FONT_FAMILY, size=16, weight="bold"),
                command=method)
            button.pack(side="left", fill="x", expand=True, padx=4, pady=6)
            self.nav_buttons[key] = button
        self.trim_page = self._page()
        self.add_page = self._page()
        self.encode_page = self._page()
        self._build_trim()
        self._build_add()
        self._build_encode()
        status_line = ctk.CTkFrame(self, fg_color="transparent")
        status_line.pack(fill="x", padx=22)
        self.start_button = ctk.CTkButton(
            status_line, text=T("BAŞLA", "START"), width=110, height=34,
            fg_color=GREEN, hover_color=GREEN_HOVER, command=self.start)
        self.start_button.pack(side="left", pady=(2, 4))
        self.status = ctk.CTkLabel(
            status_line, text=T("Hazır", "Ready"), text_color=SOFT, anchor="w")
        self.status.pack(side="left", fill="x", expand=True, padx=10)
        self.cancel_button = ctk.CTkButton(
            status_line, text=T("İPTAL", "CANCEL"), width=90, height=30,
            fg_color=RED, hover_color="#f85149",
            state="disabled", command=self.cancel_render)
        self.cancel_button.pack(side="right", pady=(2, 4))
        self.progress = ctk.CTkProgressBar(self, progress_color=GREEN)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=22, pady=(4, 16))
        self.show_trim()

    def _page(self):
        outer = ctk.CTkScrollableFrame(self.page_host, fg_color=CARD, corner_radius=12)
        return outer

    def switch_language(self, value):
        language = value.lower()
        if language == CURRENT_LANGUAGE:
            return
        data = load_app_settings()
        data["language"] = language
        save_app_settings(data)
        self.destroy()
        MediaEditorApp().mainloop()

    def _show(self, page, selected):
        for item in (self.trim_page, self.add_page, self.encode_page):
            item.pack_forget()
        for key, button in self.nav_buttons.items():
            button.configure(
                fg_color=BLUE if key == selected else CARD,
                border_color="#58a6ff" if key == selected else "#30363d")
        page.pack(fill="both", expand=True)

    def show_trim(self): self._show(self.trim_page, "trim")
    def show_add(self): self._show(self.add_page, "add")
    def show_encode(self): self._show(self.encode_page, "encode")

    def _time_option(self, parent, text, two=False):
        row = ctk.CTkFrame(parent, fg_color=INPUT, corner_radius=8)
        row.pack(fill="x", padx=14, pady=7)
        enabled = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text=text, variable=enabled).pack(
            side="left", padx=12, pady=12)
        entries = []
        for _ in range(2 if two else 1):
            entry = ctk.CTkEntry(row, width=110, placeholder_text="00:00:00")
            entry.insert(0, "00:00:00")
            entry.pack(side="left", padx=5)
            entries.append(entry)
        return enabled, entries

    def _build_trim(self):
        ctk.CTkLabel(self.trim_page, text=T("KIRPMA", "TRIM"), text_color=TEXT,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=14, pady=(14, 6))
        self.cut_start_on, start = self._time_option(
            self.trim_page, T("Baştan şu kadar kırp", "Trim from start"))
        self.cut_start = start[0]
        self.cut_middle_on, middle = self._time_option(
            self.trim_page, T("Bu zaman aralığını çıkar", "Remove this time range"), two=True)
        self.cut_middle_a, self.cut_middle_b = middle
        self.cut_end_on, end = self._time_option(
            self.trim_page, T("Sondan şu kadar kırp", "Trim from end"))
        self.cut_end = end[0]
        ctk.CTkLabel(self.trim_page,
                     text=T(
                         "Kutular varsayılan olarak kapalıdır; yalnızca seçilen kırpmalar uygulanır.",
                         "Options are off by default; only selected trims are applied."),
                     text_color=SOFT).pack(anchor="w", padx=14, pady=8)

    def _build_add(self):
        ctk.CTkLabel(self.add_page, text=T("EKLEME VE BİRLEŞTİRME", "ADD AND MERGE"),
                     text_color=TEXT,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=14, pady=(14, 10))
        video_types = [(T("MP4 videolar", "MP4 videos"), "*.mp4")]
        self.prepend_rows = FileRows(
            self.add_page, T("Ana Videonun Başına Ekle", "Add Before Main Video"),
                                     video_types)
        self.prepend_rows.pack(fill="x", padx=14)
        self.insert_rows = FileRows(
            self.add_page, T("Belirli Zamana Video Ekle", "Insert Video at Time"),
            video_types, timed=True)
        self.insert_rows.pack(fill="x", padx=14)
        self.append_rows = FileRows(
            self.add_page, T("Ana Videonun Sonuna Ekle", "Add After Main Video"),
                                    video_types)
        self.append_rows.pack(fill="x", padx=14)
        self.overlay_rows = OverlayRows(self.add_page)
        self.overlay_rows.pack(fill="x", padx=14)
        ctk.CTkLabel(
            self.add_page,
            text=T(
                "Görsel başlangıcı ve ekranda kalma süresi: HH:MM:SS",
                "Image start and display duration: HH:MM:SS"),
            text_color=SOFT).pack(anchor="w", padx=14, pady=(0, 12))

    def _combo_row(self, parent, label, values):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=5)
        ctk.CTkLabel(row, text=label, width=180, anchor="w",
                     text_color=SOFT).pack(side="left")
        combo = ctk.CTkComboBox(row, values=values, width=220,
                                command=lambda _x: self.encoding_field_changed())
        combo.pack(side="left")
        return combo

    def profile_names(self):
        profiles = self.orientation_profiles()
        saved = [
            T(f"Hızlı Ayar {i}", f"Quick Setting {i}") for i in range(1, 6)
            if f"Hızlı Ayar {i}" in profiles
        ]
        built_in = [
            T(name, PROFILE_EN[name]) for name in self.PROFILES
            if name != "Kaynak Değerleri"
        ]
        return [
            *saved, T("Kaynak Değerleri", "Source Values"),
            T("Özel", "Custom"), *built_in]

    def orientation_profiles(self):
        orientation = (
            canonical(self.orientation.get()) if hasattr(self, "orientation") else "Yatay")
        return self.quick_profiles.setdefault(orientation, {})

    def encoding_field_changed(self):
        if hasattr(self, "profile"):
            self.profile.set(T("Özel", "Custom"))
            self.show_quick_controls(True)
        self.update_estimate()

    def show_quick_controls(self, visible):
        if visible:
            if not self.quick_slot.winfo_manager():
                self.quick_slot.pack(side="left", padx=(8, 4))
                self.save_quick_button.pack(side="left", padx=4)
        else:
            self.quick_slot.pack_forget()
            self.save_quick_button.pack_forget()

    def _build_encode(self):
        encode_head = ctk.CTkFrame(self.encode_page, fg_color="transparent")
        encode_head.pack(fill="x", padx=14, pady=(14, 8))
        ctk.CTkLabel(encode_head, text="ENCODING", text_color=TEXT,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        self.pc_button = ctk.CTkButton(
            encode_head, text=T("PC Gücünü Ölç", "Measure PC Performance"), fg_color=BLUE,
            command=self.measure_pc)
        self.pc_button.pack(side="right")
        self.pc_result = ctk.CTkLabel(
            encode_head, text=T("Ölçülmedi", "Not measured"), text_color=SOFT)
        self.pc_result.pack(side="right", padx=10)
        orientation_row = ctk.CTkFrame(self.encode_page, fg_color="transparent")
        orientation_row.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(
            orientation_row, text=T("Video Yönü", "Video Orientation"),
            width=180, anchor="w",
            text_color=SOFT).pack(side="left")
        self.orientation = ctk.CTkSegmentedButton(
            orientation_row, values=[T("Yatay", "Horizontal"), T("Dikey", "Vertical")],
            command=self.change_orientation, width=220)
        self.orientation.set(T("Yatay", "Horizontal"))
        self.orientation.pack(side="left")
        profile_row = ctk.CTkFrame(self.encode_page, fg_color="transparent")
        profile_row.pack(fill="x", padx=14, pady=5)
        ctk.CTkLabel(
            profile_row, text=T("Hazır profil", "Preset"), width=180, anchor="w",
            text_color=SOFT).pack(side="left")
        self.profile = ctk.CTkComboBox(
            profile_row, values=self.profile_names(), width=220,
            command=self.apply_profile)
        self.profile.set(T("Kaynak Değerleri", "Source Values"))
        self.profile.pack(side="left")
        self.quick_slot = ctk.CTkComboBox(
            profile_row, values=[
                T(f"Hızlı Ayar {i}", f"Quick Setting {i}") for i in range(1, 6)],
            width=130, command=lambda _value: self.update_quick_slot_color())
        self.quick_slot.set(T("Hızlı Ayar 1", "Quick Setting 1"))
        self.update_quick_slot_color()
        self.save_quick_button = ctk.CTkButton(
            profile_row, text=T("Kaydet", "Save"), width=72,
            command=self.save_quick_profile)
        self.processor = self._combo_row(
            self.encode_page, T("İşleme Birimi", "Processing Unit"),
            [T("CPU (Varsayılan)", "CPU (Default)"),
             T("GPU (Donanım Hızlandırma)", "GPU (Hardware Acceleration)")])
        self.processor.set(T("CPU (Varsayılan)", "CPU (Default)"))
        self.speed_preset = self._combo_row(
            self.encode_page, T("Encoding Hızı (Kalite Kaybı)",
                                "Encoding Speed (Quality Loss)"),
            [
                T("ultrafast — Kayıp riski yüksek", "ultrafast — High loss risk"),
                T("superfast — Kayıp riski belirgin", "superfast — Noticeable loss risk"),
                T("veryfast — Kayıp riski orta", "veryfast — Medium loss risk"),
                T("faster — Kayıp riski düşük", "faster — Low loss risk"),
                T("fast — Kayıp riski çok düşük", "fast — Very low loss risk"),
                T("medium — Kalite öncelikli", "medium — Quality focused"),
                T("slow — Kalite maksimum", "slow — Maximum quality"),
            ])
        self.speed_preset.set(T("fast — Kayıp riski çok düşük",
                                "fast — Very low loss risk"))
        self.codec = self._combo_row(self.encode_page, T("Video codec", "Video codec"),
                                     ["h264", "h265"])
        self.resolution = self._combo_row(
            self.encode_page, T("Çözünürlük", "Resolution"),
            self.resolution_values(T("Yatay", "Horizontal")))
        self.fps = self._combo_row(self.encode_page, "FPS",
                                  ["24", "25", "30", "50", "60"])
        self.bitrate = self._combo_row(self.encode_page, "Video bitrate (Mbps)",
                                      ["1", "2", "3", "5", "8", "9", "10", "16", "25", "35"])
        self.audio_codec = self._combo_row(
            self.encode_page, T("Ses codec", "Audio codec"),
                                           ["aac", "mp3"])
        self.audio_bitrate = self._combo_row(
            self.encode_page, T("Ses bitrate (kbps)", "Audio bitrate (kbps)"),
                                             ["96", "128", "160", "192", "256", "320"])
        self.estimate_label = ctk.CTkLabel(
            self.encode_page,
            text=T("Tahmini çıktı: Ana video seçilmedi",
                   "Estimated output: No main video selected"),
            text_color=GREEN, font=ctk.CTkFont(size=13, weight="bold"))
        self.estimate_label.pack(anchor="w", padx=14, pady=14)

    def choose_source(self):
        path = filedialog.askopenfilename(filetypes=[("MP4 videolar", "*.mp4")])
        if not path:
            return
        try:
            info = probe(path)
        except MontageError as exc:
            messagebox.showerror(
                T("Ana video okunamadı", "Main video could not be read"),
                error_text(str(exc)))
            return
        self.source, self.info = Path(path), info
        self.source_label.configure(text=self.source.name, text_color=TEXT)
        self.info_label.configure(
            text=f"{format_time(info.duration)}  •  {info.width}×{info.height}  •  "
                 f"{info.fps:g} FPS")
        detected_orientation = "Yatay" if info.width >= info.height else "Dikey"
        self.orientation.set(detected_orientation)
        self.change_orientation(detected_orientation)
        self.apply_source_profile()

    def resolution_values(self, orientation):
        orientation = canonical(orientation)
        horizontal = [
            "854x480", "1280x720", "1920x1080", "2560x1440", "3840x2160"]
        values = (
            ["x".join(reversed(value.split("x"))) for value in horizontal]
            if orientation == "Dikey" else horizontal
        )
        if self.info:
            width, height = self.info.width, self.info.height
            if (orientation == "Dikey" and width > height) or (
                    orientation == "Yatay" and width < height):
                width, height = height, width
            source_value = f"{width}x{height}"
            if source_value in values:
                values.remove(source_value)
            values.insert(0, source_value)
        return values

    def change_orientation(self, orientation):
        orientation = canonical(orientation)
        display_orientation = T(orientation, "Vertical" if orientation == "Dikey" else "Horizontal")
        if self.orientation.get() != display_orientation:
            self.orientation.set(display_orientation)
        self.resolution.configure(values=self.resolution_values(orientation))
        current = self.resolution.get()
        try:
            width, height = current.lower().split("x")
            if (orientation == "Dikey" and int(width) > int(height)) or (
                    orientation == "Yatay" and int(width) < int(height)):
                self.resolution.set(f"{height}x{width}")
        except ValueError:
            self.resolution.set(self.resolution_values(orientation)[2])
        if hasattr(self, "profile"):
            self.profile.configure(values=self.profile_names())
        if hasattr(self, "quick_slot"):
            self.update_quick_slot_color()
        self.encoding_field_changed()

    def choose_output_dir(self):
        selected = filedialog.askdirectory(
            initialdir=str(self.output_dir))
        if selected:
            self.output_dir = Path(selected)
            save_output_dir(self.output_dir)
            self.output_button.configure(text=self.output_button_text())

    def output_button_text(self):
        name = self.output_dir.name or str(self.output_dir)
        return f"{T('Çıkış Klasörü', 'Output Folder')}: {name}"

    def apply_source_profile(self):
        if not self.info:
            return
        bitrate = self.info.video_bitrate / 1_000_000 if self.info.video_bitrate else 8
        audio = self.info.audio_bitrate // 1000 if self.info.audio_bitrate else 192
        values = (
            "h265" if "265" in self.info.video_codec or "hevc" in self.info.video_codec else "h264",
            f"{self.info.width}x{self.info.height}", f"{self.info.fps:g}",
            f"{max(0.1, bitrate):g}", self.info.audio_codec if self.info.audio_codec in ("aac", "mp3") else "aac",
            str(audio),
        )
        self._set_encoding(values)
        self.profile.set(T("Kaynak Değerleri", "Source Values"))
        self.show_quick_controls(False)

    def current_profile_data(self):
        return {
            "orientation": canonical(self.orientation.get()),
            "processor": "gpu" if self.processor.get().startswith("GPU") else "cpu",
            "preset": self.speed_preset.get().split(" ", 1)[0],
            "codec": self.codec.get(),
            "resolution": self.resolution.get(),
            "fps": self.fps.get(),
            "bitrate": self.bitrate.get(),
            "audio_codec": self.audio_codec.get(),
            "audio_bitrate": self.audio_bitrate.get(),
        }

    def update_quick_slot_color(self):
        filled = canonical(self.quick_slot.get()) in self.orientation_profiles()
        self.quick_slot.configure(
            fg_color="#274b6d" if filled else "#30363d",
            button_color="#345f88" if filled else "#484f58")

    def save_quick_profile(self):
        slot = canonical(self.quick_slot.get())
        profiles = self.orientation_profiles()
        if slot in profiles and not messagebox.askyesno(
                T("Hızlı ayarın üzerine yazılsın mı?", "Overwrite quick setting?"),
                T(f"{slot} daha önce kaydedilmiş. Üzerine yazılsın mı?",
                  f"{slot.replace('Hızlı Ayar', 'Quick Setting')} is already saved. Overwrite it?")):
            return
        profiles[slot] = self.current_profile_data()
        data = load_app_settings()
        data["quick_profiles"] = self.quick_profiles
        save_app_settings(data)
        self.profile.configure(values=self.profile_names())
        slot_number = slot.rsplit(" ", 1)[-1]
        self.profile.set(T(slot, f"Quick Setting {slot_number}"))
        self.update_quick_slot_color()
        self.show_quick_controls(False)
        messagebox.showinfo(
            T("Hızlı ayar kaydedildi", "Quick setting saved"),
            T(f"{slot} kaydedildi.",
              f"{slot.replace('Hızlı Ayar', 'Quick Setting')} was saved."))

    def apply_profile(self, name):
        name = canonical(name)
        if name == "Özel":
            self.show_quick_controls(True)
            return
        profiles = self.orientation_profiles()
        if name in profiles:
            saved = profiles[name]
            saved_orientation = canonical(saved["orientation"])
            self.orientation.set(T(
                saved_orientation,
                "Vertical" if saved_orientation == "Dikey" else "Horizontal"))
            self.change_orientation(saved_orientation)
            saved_processor = saved["processor"]
            if saved_processor in ("cpu", "gpu"):
                self.processor.set(
                    T("GPU (Donanım Hızlandırma)", "GPU (Hardware Acceleration)")
                    if saved_processor == "gpu"
                    else T("CPU (Varsayılan)", "CPU (Default)"))
            else:
                self.processor.set(saved_processor)
            saved_preset = saved["preset"]
            preset_options = list(self.speed_preset.cget("values"))
            preset_display = next(
                (option for option in preset_options if option.startswith(saved_preset + " ")),
                saved_preset)
            self.speed_preset.set(preset_display)
            self._set_encoding((
                saved["codec"], saved["resolution"], saved["fps"],
                saved["bitrate"], saved["audio_codec"], saved["audio_bitrate"]))
            slot_number = name.rsplit(" ", 1)[-1]
            self.profile.set(T(name, f"Quick Setting {slot_number}"))
            self.show_quick_controls(False)
            return
        self.show_quick_controls(False)
        values = self.PROFILES[name]
        if values is None:
            self.apply_source_profile()
        else:
            adjusted = list(values)
            if canonical(self.orientation.get()) == "Dikey":
                width, height = adjusted[1].split("x")
                adjusted[1] = f"{height}x{width}"
            self._set_encoding(adjusted)

    def _set_encoding(self, values):
        for widget, value in zip(
                (self.codec, self.resolution, self.fps, self.bitrate,
                 self.audio_codec, self.audio_bitrate), values):
            widget.set(str(value))
        self.update_estimate()

    def settings(self):
        try:
            width, height = map(int, self.resolution.get().lower().split("x"))
            return EncodeSettings(
                self.codec.get(), width, height, float(self.fps.get()),
                float(self.bitrate.get()), self.audio_codec.get(),
                int(self.audio_bitrate.get()),
                device="gpu" if self.processor.get().startswith("GPU") else "cpu",
                preset=self.speed_preset.get().split(" ", 1)[0])
        except ValueError as exc:
            raise MontageError("Encoding alanlarından biri geçersiz.") from exc

    def make_job(self, output=None):
        if not self.source:
            raise MontageError("Önce ana MP4 dosyasını seçin.")
        output_dir = self.output_dir
        middle = None
        if self.cut_middle_on.get():
            middle = (parse_time(self.cut_middle_a.get()),
                      parse_time(self.cut_middle_b.get()))
        return Job(
            source=self.source,
            output=Path(output) if output else output_dir / (
                "Edit_" + self.source.name),
            cut_start=parse_time(self.cut_start.get()) if self.cut_start_on.get() else 0,
            cut_middle=middle,
            cut_end=parse_time(self.cut_end.get()) if self.cut_end_on.get() else 0,
            prepend=self.prepend_rows.paths(),
            append=self.append_rows.paths(),
            inserts=self.insert_rows.timed_paths(),
            overlays=self.overlay_rows.overlays(),
            encode=self.settings(),
        )

    def update_estimate(self):
        if not self.info:
            return
        try:
            _, duration = validate(self.make_job())
            settings = self.settings()
            estimate = estimated_bytes(duration, settings)
            key = self.benchmark_key(settings)
            factor = self.benchmark_factors.get(key)
            process_seconds = (
                duration * factor if factor is not None
                else estimated_process_seconds(duration, settings)
            )
            calibrated = "ölçümlü" if factor is not None else "ilk tahmin"
            self.estimate_label.configure(
                text=(
                    f"{T('Tahmini çıktı', 'Estimated output')}: ≈ {human_size(estimate)}  •  "
                    f"{T('Çıktı süresi', 'Output duration')}: {format_time(duration)}  •  "
                    f"{T('Tahmini işlem süresi', 'Estimated processing time')}: "
                    f"≈ {format_time(process_seconds)} "
                    f"({T(calibrated, 'measured' if calibrated == 'ölçümlü' else 'initial estimate')})"
                ))
        except Exception:
            self.estimate_label.configure(
                text=T("Tahmin için ayarları tamamlayın",
                       "Complete the settings to calculate an estimate"))

    @staticmethod
    def benchmark_key(settings):
        return (settings.device, settings.codec, settings.width,
                settings.height, settings.fps, settings.preset)

    def measure_pc(self):
        if not self.source:
            messagebox.showerror(
                T("PC gücü ölçülemedi", "PC performance could not be measured"),
                T("Önce ana MP4 dosyasını seçin.", "Select the main MP4 file first."))
            return
        try:
            settings = self.settings()
        except MontageError as exc:
            messagebox.showerror(
                T("PC gücü ölçülemedi", "PC performance could not be measured"),
                error_text(str(exc)))
            return
        self.pc_button.configure(
            state="disabled", text=T("Ölçülüyor…", "Measuring…"))
        self.pc_result.configure(
            text=T("3 test yapılıyor: 0/3", "Running 3 tests: 0/3"),
            text_color=SOFT)
        threading.Thread(
            target=self._benchmark_worker, args=(settings,), daemon=True).start()

    def _benchmark_worker(self, settings):
        try:
            factors = []
            for run_number in range(1, 4):
                factors.append(benchmark_encode(self.source, settings))
                self.events.put(("benchmark_progress", run_number))
            factor = sum(factors) / len(factors)
            self.events.put(("benchmark_done", (settings, factor)))
        except Exception as exc:
            self.events.put(("benchmark_error", str(exc)))

    def start(self):
        if hasattr(self, "worker") and self.worker.is_alive():
            return
        try:
            job = self.make_job()
            _, duration = validate(job)
            size = human_size(estimated_bytes(duration, job.encode))
        except MontageError as exc:
            messagebox.showerror(
                T("İşlem başlatılamadı", "Processing could not start"),
                error_text(str(exc)))
            return
        if job.output.exists() and not messagebox.askyesno(
                T("Dosya mevcut", "File already exists"),
                T("Seçilen çıktı dosyasının üzerine yazılsın mı?",
                  "Overwrite the selected output file?")):
            return
        self.cancel_event.clear()
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.configure(
            text=f"{T('İşleniyor… Tahmini çıktı', 'Processing… Estimated output')} {size}",
            text_color=TEXT)
        self.progress.set(0)
        self.worker = threading.Thread(target=self._render_worker, args=(job,),
                                       daemon=True)
        self.worker.start()

    def cancel_render(self):
        if hasattr(self, "worker") and self.worker.is_alive():
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.configure(
                text=T("İşlem iptal ediliyor…", "Cancelling…"), text_color=SOFT)

    def _render_worker(self, job):
        try:
            render(job, lambda value: self.events.put(("progress", value)),
                   self.cancel_event.is_set)
            self.events.put(("done", job.output))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.progress.set(value)
                    self.status.configure(
                        text=f"{T('İşleniyor', 'Processing')}… %{value * 100:.1f}")
                elif kind == "done":
                    self.start_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.status.configure(
                        text=f"{T('Tamamlandı', 'Completed')}: {value}",
                        text_color=GREEN)
                    messagebox.showinfo(
                        T("Montaj tamamlandı", "Editing completed"),
                        f"{T('Çıktı oluşturuldu', 'Output created')}:\n{value}")
                elif kind == "benchmark_done":
                    settings, factor = value
                    self.benchmark_factors[self.benchmark_key(settings)] = factor
                    speed = 1 / factor
                    self.pc_button.configure(
                        state="normal",
                        text=T("PC Gücünü Ölç", "Measure PC Performance"))
                    self.pc_result.configure(
                        text=f"{speed:.2f}× {T('gerçek zaman', 'real time')}",
                        text_color=GREEN)
                    self.update_estimate()
                elif kind == "benchmark_progress":
                    self.pc_result.configure(
                        text=T(f"3 test yapılıyor: {value}/3",
                               f"Running 3 tests: {value}/3"), text_color=SOFT)
                elif kind == "benchmark_error":
                    self.pc_button.configure(
                        state="normal",
                        text=T("PC Gücünü Ölç", "Measure PC Performance"))
                    self.pc_result.configure(
                        text=T("Ölçüm başarısız", "Measurement failed"),
                        text_color=RED)
                    messagebox.showerror(
                        T("PC gücü ölçülemedi",
                          "PC performance could not be measured"),
                        error_text(value))
                else:
                    self.start_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.progress.set(0)
                    if "iptal edildi" in value.lower():
                        self.status.configure(text=T(
                            "İşlem iptal edildi; yarım çıktı silindi.",
                            "Processing cancelled; partial output was deleted."),
                                              text_color=SOFT)
                    else:
                        self.status.configure(
                            text=T("İşlem başarısız", "Processing failed"),
                            text_color=RED)
                        messagebox.showerror(
                            T("İşlem tamamlanamadı", "Processing could not complete"),
                            error_text(value))
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    MediaEditorApp().mainloop()
