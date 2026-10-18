import os
import re
import sys
import threading
import subprocess
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "Dubbing Studio"
BG = "#F5F6F8"
CARD = "#FFFFFF"
TEXT = "#17191D"
MUTED = "#6B7280"
RED = "#D92332"
RED_HOVER = "#B91C2A"
BORDER = "#E3E5E8"
SOFT_RED = "#FFF0F1"
SUBTITLE_FROM_YOUTUBE = "youtube"  # tells the engine to fetch subtitles from the video

# Dropdown label -> edge-tts voice id. The engine checks that the subtitle script matches the voice.
VOICE_CHOICES = {
    "Hindi — Swara (female)": "hi-IN-SwaraNeural",
    "Hindi — Madhur (male)": "hi-IN-MadhurNeural",
    "Gujarati — Dhwani (female)": "gu-IN-DhwaniNeural",
    "Gujarati — Niranjan (male)": "gu-IN-NiranjanNeural",
    "Bengali — Tanishaa (female)": "bn-IN-TanishaaNeural",
    "Bengali — Bashkar (male)": "bn-IN-BashkarNeural",
    "Marathi — Aarohi (female)": "mr-IN-AarohiNeural",
    "Marathi — Manohar (male)": "mr-IN-ManoharNeural",
    "Tamil — Pallavi (female)": "ta-IN-PallaviNeural",
    "Tamil — Valluvar (male)": "ta-IN-ValluvarNeural",
    "Telugu — Shruti (female)": "te-IN-ShrutiNeural",
    "Telugu — Mohan (male)": "te-IN-MohanNeural",
    "Kannada — Sapna (female)": "kn-IN-SapnaNeural",
    "Kannada — Gagan (male)": "kn-IN-GaganNeural",
    "Malayalam — Sobhana (female)": "ml-IN-SobhanaNeural",
    "Malayalam — Midhun (male)": "ml-IN-MidhunNeural",
    "Urdu — Gul (female)": "ur-IN-GulNeural",
    "Urdu — Salman (male)": "ur-IN-SalmanNeural",
    "Nepali — Hemkala (female)": "ne-NP-HemkalaNeural",
    "Nepali — Sagar (male)": "ne-NP-SagarNeural",
    "English (India) — Neerja (female)": "en-IN-NeerjaNeural",
    "English (India) — Prabhat (male)": "en-IN-PrabhatNeural",
}

# Only these caption languages are listed after Check (Indian languages with a voice above).
INDIAN_CAPTION_CODES = {"hi", "gu", "bn", "mr", "ta", "te", "kn", "ml", "ur", "ne", "en-in"}


def is_indian_caption(lang: str) -> bool:
    code = lang.lower()
    return code in INDIAN_CAPTION_CODES or code.split("-")[0] in INDIAN_CAPTION_CODES - {"en-in"}


class DubbingApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1120x660")
        self.root.minsize(980, 600)
        self.root.configure(bg=BG)
        self.output_file = None
        self.is_running = False

        self.youtube_var = tk.StringVar()
        self.subtitle_var = tk.StringVar(value="")
        self.voice_var = tk.StringVar(value="Hindi — Swara (female)")
        self.output_var = tk.StringVar(value=str(Path.home() / "Videos" / "Dubbed.mp4"))
        self.status_var = tk.StringVar(value="Ready when you are.")
        self.progress_var = tk.DoubleVar(value=0)
        self.caption_choice_var = tk.StringVar(value="Select Language")
        self.caption_tracks = {}
        self.caption_title = ""
        self.caption_busy = False

        self._setup_styles()
        self._build_ui()
        self._sync_output_controls()

    def _setup_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Red.Horizontal.TProgressbar",
            troughcolor="#ECEEF1",
            background=RED,
            bordercolor="#ECEEF1",
            lightcolor=RED,
            darkcolor=RED,
            thickness=9,
        )
        style.configure(
            "TCombobox",
            fieldbackground="#FFFFFF",
            background="#FFFFFF",
            foreground=TEXT,
            arrowcolor=TEXT,
            padding=8,
        )

    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        header.pack(fill="x")
        brand = tk.Frame(header, bg=CARD)
        brand.pack(fill="x", padx=20, pady=12)

        mark = tk.Label(
            brand, text="▶", font=("Segoe UI", 16, "bold"),
            bg=RED, fg="white", width=2, height=1, padx=2, pady=5
        )
        mark.pack(side="left", padx=(0, 10))
        titles = tk.Frame(brand, bg=CARD)
        titles.pack(side="left", fill="x", expand=True)
        tk.Label(
            titles, text="Dubbing Studio",
            font=("Segoe UI", 18, "bold"), bg=CARD, fg=TEXT
        ).pack(anchor="w")
        tk.Label(
            titles, text="Turn a YouTube lesson into a dubbed video",
            font=("Segoe UI", 9), bg=CARD, fg=MUTED
        ).pack(anchor="w", pady=(2, 0))
        tk.Label(
            brand, text="PYTHON DESKTOP",
            font=("Segoe UI", 7, "bold"), bg="#F2F3F5", fg="#555B65",
            padx=8, pady=5
        ).pack(side="right", anchor="n")

        # Main content - two column layout
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=16, pady=10)

        # Left column: Inputs
        left_col = tk.Frame(main, bg=BG)
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 8))
        left_col.grid_rowconfigure(0, weight=1)
        left_col.grid_columnconfigure(0, weight=1)

        # Input card
        card = tk.Frame(left_col, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=12, pady=10)

        self._section_heading(inner, "1", "YouTube video URL & captions")
        url_row = tk.Frame(inner, bg=CARD)
        url_row.pack(fill="x", pady=(4, 6))
        self.youtube_entry = self._entry(url_row, self.youtube_var, "https://www.youtube.com/watch?v=...")
        self.youtube_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.check_captions_button = self._button(url_row, "Check", self.check_captions, secondary=True, width=10)
        self.check_captions_button.pack(side="left", padx=(6, 0))

        caption_row = tk.Frame(inner, bg=CARD)
        caption_row.pack(fill="x", pady=(0, 6))
        self.caption_combo = ttk.Combobox(
            caption_row, textvariable=self.caption_choice_var, state="readonly",
            values=["Select Language"]
        )
        self.caption_combo.pack(side="left", fill="x", expand=True)
        self.download_caption_button = self._button(caption_row, "Download", self.download_caption, secondary=True, width=10)
        self.download_caption_button.pack(side="left", padx=(6, 0))
        self.download_caption_button.configure(state="disabled")

        self.caption_status_label = tk.Label(
            inner, text="No captions checked yet.", font=("Segoe UI", 8),
            bg=CARD, fg=MUTED, anchor="w"
        )
        self.caption_status_label.pack(fill="x", pady=(0, 6))

        self._section_heading(inner, "2", "Subtitle file")
        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x", pady=(4, 6))
        self.subtitle_entry = self._entry(row, self.subtitle_var, r".vtt/.srt or URL")
        self.subtitle_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self._button(row, "Browse", self.browse_subtitle, secondary=True, width=8).pack(side="left", padx=(6, 0))

        # Settings
        self._section_heading(inner, "3", "Voice & output")
        settings = tk.Frame(inner, bg=CARD)
        settings.pack(fill="x", pady=(4, 0))
        left = tk.Frame(settings, bg=CARD)
        left.pack(side="left", fill="x", expand=True, padx=(0, 6))
        right = tk.Frame(settings, bg=CARD)
        right.pack(side="left", fill="x", expand=True, padx=(6, 0))

        tk.Label(left, text="Voice", font=("Segoe UI", 8), bg=CARD, fg=MUTED).pack(anchor="w")
        self.voice_combo = ttk.Combobox(
            left, textvariable=self.voice_var, state="readonly",
            values=list(VOICE_CHOICES.keys()),
        )
        self.voice_combo.pack(fill="x", pady=(2, 0), ipady=2)

        tk.Label(right, text="Output path", font=("Segoe UI", 8), bg=CARD, fg=MUTED).pack(anchor="w")
        outrow = tk.Frame(right, bg=CARD)
        outrow.pack(fill="x", pady=(2, 0))
        self.output_entry = self._entry(outrow, self.output_var, r"Dubbed.mp4")
        self.output_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self._button(outrow, "Save", self.browse_output, secondary=True, width=6).pack(side="left", padx=(4, 0))

        # Right column: Actions/Progress
        right_col = tk.Frame(main, bg=BG)
        right_col.pack(side="left", fill="y", padx=(8, 0))
        right_col.configure(width=360)
        right_col.pack_propagate(False)

        # Generate button
        self.generate_button = self._button(right_col, "Generate dubbed video", self.start_generation, primary=True)
        self.generate_button.pack(fill="x", ipady=3, pady=(0, 6))

        # Progress card
        progress_card = tk.Frame(right_col, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        progress_card.pack(fill="x", pady=(0, 6))
        pinner = tk.Frame(progress_card, bg=CARD)
        pinner.pack(fill="x", padx=10, pady=8)
        top = tk.Frame(pinner, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text="Progress", font=("Segoe UI", 9, "bold"), bg=CARD, fg=TEXT).pack(side="left")
        self.percent_label = tk.Label(top, text="0%", font=("Segoe UI", 8, "bold"), bg=CARD, fg=RED)
        self.percent_label.pack(side="right")
        self.progress = ttk.Progressbar(pinner, style="Red.Horizontal.TProgressbar", maximum=100, variable=self.progress_var)
        self.progress.pack(fill="x", pady=(6, 4))
        tk.Label(pinner, textvariable=self.status_var, font=("Segoe UI", 8), bg=CARD, fg=MUTED, anchor="w", wraplength=280).pack(fill="x")

        # Info note
        note = tk.Frame(right_col, bg=SOFT_RED, highlightbackground="#F7D5D8", highlightthickness=1)
        note.pack(fill="x", pady=(0, 6))
        tk.Label(note, text="i", font=("Segoe UI", 9, "bold"), bg=SOFT_RED, fg=RED, width=2).pack(side="left", padx=(6, 0), pady=4)
        tk.Label(
            note,
            text="Use a subtitle track with timestamps. The dubbing engine must preserve timing and avoid forcing long lines into short slots.",
            font=("Segoe UI", 7), bg=SOFT_RED, fg="#6D3036", justify="left",
            wraplength=320
        ).pack(side="left", fill="x", expand=True, padx=(2, 6), pady=4)

        # Output actions
        actions = tk.Frame(right_col, bg=BG)
        actions.pack(fill="x")
        self.open_video_button = self._button(actions, "Open video", self.open_video, secondary=True)
        self.open_video_button.pack(fill="x", pady=(0, 4))
        self.open_folder_button = self._button(actions, "Open folder", self.open_output_folder, secondary=True)
        self.open_folder_button.pack(fill="x", pady=(0, 4))
        self.open_url_button = self._button(actions, "Open YouTube", self.open_youtube, secondary=True)
        self.open_url_button.pack(fill="x")

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", padx=20, pady=(0, 10))
        tk.Label(
            footer, text="Dubbing Studio  •  Local Windows interface",
            font=("Segoe UI", 7), bg=BG, fg="#9298A2"
        ).pack(side="left")
        tk.Label(
            footer, text="Red • Black • White",
            font=("Segoe UI", 7), bg=BG, fg="#9298A2"
        ).pack(side="right")

    def _section_heading(self, parent, number, title):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", anchor="w")
        tk.Label(
            row, text=number, font=("Segoe UI", 10, "bold"), bg=RED, fg="white",
            width=2, padx=3, pady=3
        ).pack(side="left", padx=(0, 10))
        tk.Label(row, text=title, font=("Segoe UI", 11, "bold"), bg=CARD, fg=TEXT).pack(side="left")

    def _field_label(self, parent, text):
        tk.Label(parent, text=text, font=("Segoe UI", 9), bg=CARD, fg=MUTED).pack(anchor="w", pady=(7, 0))

    def _entry(self, parent, variable, placeholder):
        entry = tk.Entry(
            parent, textvariable=variable, font=("Segoe UI", 10),
            bg="#FFFFFF", fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=RED,
        )
        entry._placeholder = placeholder
        return entry

    def _button(self, parent, text, command, primary=False, secondary=False, width=None):
        if primary:
            bg, fg, active = RED, "white", RED_HOVER
            font = ("Segoe UI", 11, "bold")
            relief = "flat"
            padx, pady = 14, 11
        elif secondary:
            bg, fg, active = "#FFFFFF", TEXT, "#F2F3F5"
            font = ("Segoe UI", 9, "bold")
            relief = "solid"
            padx, pady = 11, 8
        else:
            bg, fg, active = RED, "white", RED_HOVER
            font = ("Segoe UI", 10, "bold")
            relief = "flat"
            padx, pady = 10, 7
        btn = tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=active, activeforeground=fg,
            font=font, relief=relief, bd=1 if secondary else 0,
            highlightthickness=0, cursor="hand2", padx=padx, pady=pady,
            width=width,
        )
        if secondary:
            btn.configure(highlightbackground=BORDER)
        return btn

    def _engine_path(self):
        return Path(__file__).with_name("dubbing_engine.py")

    def _valid_youtube_url(self):
        url = self.youtube_var.get().strip()
        if not url or not re.match(r"^https?://", url, re.I):
            messagebox.showerror("YouTube URL required", "Enter a valid YouTube video URL first.")
            return None
        return url

    def check_captions(self):
        if self.caption_busy:
            return
        url = self._valid_youtube_url()
        if not url:
            return
        engine = self._engine_path()
        if not engine.exists():
            messagebox.showerror("Engine not found", "dubbing_engine.py must be in the same folder as app.py.")
            return
        self.caption_busy = True
        self.check_captions_button.configure(state="disabled", text="Checking…")
        self.download_caption_button.configure(state="disabled")
        self.caption_status_label.configure(text="Checking caption languages on YouTube…", fg=MUTED)
        threading.Thread(target=self._check_captions_worker, args=(engine, url), daemon=True).start()

    def _check_captions_worker(self, engine, url):
        try:
            proc = subprocess.run(
                [sys.executable, str(engine), "--url", url, "--list-subs"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180
            )
            if proc.returncode != 0:
                lines = [x.strip() for x in (proc.stdout + "\n" + proc.stderr).splitlines() if x.strip()]
                reason = next((x[6:].strip() for x in reversed(lines) if x.startswith("ERROR:")), None)
                raise RuntimeError(reason or "Could not read captions. Check the URL and internet connection.")
            title = ""
            tracks = {}
            for line in proc.stdout.splitlines():
                parts = line.strip().split("|", 3)
                if parts[0] == "TITLE" and len(parts) > 1:
                    title = parts[1]
                elif parts[0] == "SUB" and len(parts) == 4:
                    lang, kind, name = parts[1], parts[2], parts[3]
                    if not is_indian_caption(lang):
                        continue
                    label_name = name if name and name != lang else lang
                    source = "Uploaded" if kind == "manual" else "Auto-generated"
                    label = f"{label_name} ({lang}) — {source}"
                    tracks[label] = {"lang": lang, "kind": kind}
            self.root.after(0, self._captions_loaded, title, tracks)
        except Exception as exc:
            self.root.after(0, self._captions_failed, str(exc))

    def _captions_loaded(self, title, tracks):
        self.caption_busy = False
        self.check_captions_button.configure(state="normal", text="Check captions")
        self.caption_title = title
        self.caption_tracks = tracks
        labels = list(tracks.keys())
        self.caption_combo.configure(values=["Select Language"] + labels)
        self.caption_choice_var.set("Select Language")
        self.download_caption_button.configure(state="normal" if labels else "disabled")
        if labels:
            self.caption_status_label.configure(
                text=f"Found {len(labels)} caption track(s)" + (f" for: {title}" if title else "") + ". Select a language and download it.",
                fg="#267647"
            )
        else:
            self.caption_status_label.configure(text="No downloadable captions were found for this video.", fg=RED)
            messagebox.showwarning("No captions found", "YouTube did not provide downloadable subtitle tracks for this video.")

    def _captions_failed(self, error):
        self.caption_busy = False
        self.check_captions_button.configure(state="normal", text="Check captions")
        self.download_caption_button.configure(state="disabled")
        self.caption_status_label.configure(text="Could not retrieve captions.", fg=RED)
        messagebox.showerror("Caption check failed", error)

    def download_caption(self):
        if self.caption_busy:
            return
        url = self._valid_youtube_url()
        if not url:
            return
        label = self.caption_choice_var.get()
        track = self.caption_tracks.get(label)
        if not track:
            messagebox.showwarning("Select a language", "Check captions and select a language first.")
            return
        suggested = f"{re.sub(r'[^A-Za-z0-9_-]+', '_', track['lang'])}_captions.srt"
        destination = filedialog.asksaveasfilename(
            title="Save downloaded caption",
            defaultextension=".srt",
            initialfile=suggested,
            filetypes=[("SubRip subtitles", "*.srt")]
        )
        if not destination:
            return
        engine = self._engine_path()
        self.caption_busy = True
        self.check_captions_button.configure(state="disabled")
        self.download_caption_button.configure(state="disabled", text="Downloading…")
        self.caption_status_label.configure(text="Downloading selected caption…", fg=MUTED)
        threading.Thread(
            target=self._download_caption_worker,
            args=(engine, url, track["lang"], track["kind"], Path(destination)),
            daemon=True
        ).start()

    def _download_caption_worker(self, engine, url, lang, kind, destination):
        import time
        max_retries = 4
        for attempt in range(max_retries):
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                proc = subprocess.run(
                    [sys.executable, str(engine), "--url", url, "--get-sub", lang,
                     "--sub-kind", kind, "--sub-dir", str(destination.parent)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300
                )
                if proc.returncode != 0:
                    lines = [x.strip() for x in (proc.stdout + "\n" + proc.stderr).splitlines() if x.strip()]
                    reason = next((x[6:].strip() for x in reversed(lines) if x.startswith("ERROR:")), None)
                    if attempt < max_retries - 1 and "429" in (reason or ""):
                        wait = 15 * 2 ** attempt
                        self.root.after(0, self.caption_status_label.configure, {"text": f"Rate limited, retrying in {wait}s...", "fg": "#E67E22"})
                        time.sleep(wait)
                        continue
                    raise RuntimeError(reason or "Caption download failed.")
                saved_line = next((x for x in proc.stdout.splitlines() if x.startswith("SAVED|")), None)
                if not saved_line:
                    raise RuntimeError("The downloader did not return the saved caption path.")
                source = Path(saved_line.split("|", 1)[1].strip())
                if not source.exists():
                    raise RuntimeError(f"Downloaded caption file was not found: {source}")
                # Respect the filename and extension chosen by the user.
                if destination.suffix.lower() != ".srt":
                    destination = destination.with_suffix(".srt")
                if source.resolve() != destination.resolve():
                    if destination.exists():
                        destination.unlink()
                    source.replace(destination)
                self.root.after(0, self._caption_downloaded, str(destination))
                return
            except RuntimeError as e:
                if attempt < max_retries - 1 and "429" in str(e):
                    wait = 2 ** attempt
                    self.root.after(0, self.caption_status_label.configure, {"text": f"Rate limited, retrying in {wait}s...", "fg": "#E67E22"})
                    time.sleep(wait)
                    continue
                self.root.after(0, self._caption_download_failed, str(e))
                return
        self.root.after(0, self._caption_download_failed, RuntimeError("Max retries exceeded for caption download"))

    def _caption_downloaded(self, path):
        self.caption_busy = False
        self.check_captions_button.configure(state="normal", text="Check captions")
        self.download_caption_button.configure(state="normal", text="Download caption")
        self.subtitle_var.set(path)
        self.caption_status_label.configure(text=f"Caption downloaded: {path}", fg="#267647")
        messagebox.showinfo("Caption downloaded", f"Subtitle saved successfully:\n{path}\n\nThe subtitle path has been filled into Step 2.")

    def _caption_download_failed(self, error):
        self.caption_busy = False
        self.check_captions_button.configure(state="normal", text="Check captions")
        self.download_caption_button.configure(state="normal", text="Download caption")
        self.caption_status_label.configure(text="Caption download failed.", fg=RED)
        messagebox.showerror("Caption download failed", error)

    def browse_subtitle(self):
        path = filedialog.askopenfilename(
            title="Choose subtitle file",
            filetypes=[("Subtitle files", "*.vtt *.srt"), ("All files", "*.*")]
        )
        if path:
            self.subtitle_var.set(path)

    def browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Save dubbed video as",
            defaultextension=".mp4",
            initialfile=Path(self.output_var.get()).name or "Dubbed.mp4",
            filetypes=[("MP4 video", "*.mp4")]
        )
        if path:
            self.output_var.set(path)
            self._sync_output_controls()

    def start_generation(self):
        if self.is_running:
            return
        url = self.youtube_var.get().strip()
        subtitle = self.subtitle_var.get().strip()
        output = self.output_var.get().strip()

        if not url or not re.match(r"^https?://", url, re.I):
            messagebox.showerror("YouTube URL required", "Enter a valid YouTube video URL.")
            return
        if not subtitle:
            messagebox.showerror("Subtitle required", "Enter a .vtt/.srt file path or direct URL.")
            return
        if subtitle.lower() in (SUBTITLE_FROM_YOUTUBE, "auto"):
            subtitle = SUBTITLE_FROM_YOUTUBE
        elif not re.match(r"^https?://", subtitle, re.I):
            path = Path(subtitle).expanduser()
            if not path.exists():
                messagebox.showerror("Subtitle file not found", "The local subtitle path does not exist. Browse to a VTT or SRT file.")
                return
            if path.suffix.lower() not in (".vtt", ".srt"):
                messagebox.showerror("Unsupported subtitle format", "Choose a .vtt or .srt subtitle file.")
                return
        elif not re.search(r"\.(vtt|srt)(?:[?#].*)?$", subtitle, re.I):
            messagebox.showwarning("Check subtitle URL", "The URL does not end in .vtt or .srt. Make sure it is a direct subtitle-file URL.")
            return
        if not output:
            messagebox.showerror("Output path required", "Choose where to save the generated MP4.")
            return

        # Pre-flight validation: check subtitle has content
        if subtitle.lower() not in (SUBTITLE_FROM_YOUTUBE, "auto"):
            p = Path(subtitle).expanduser()
            if p.exists():
                try:
                    txt = p.read_text(encoding="utf-8", errors="replace")
                    if not txt.strip():
                        messagebox.showerror("Subtitle empty", "The subtitle file is empty. Select a file with actual caption text.")
                        return
                except Exception:
                    pass

        engine = Path(__file__).with_name("dubbing_engine.py")
        if not engine.exists():
            messagebox.showinfo(
                "UI ready — dubbing engine not connected",
                "Your inputs are valid, but the generation engine is not included in this UI-only starter project.\n\n"
                "Next, connect a dubbing backend that uses the selected subtitle timestamps and natural pacing. "
                "This avoids the fast/cut-off speech problems from the earlier prototype."
            )
            self.status_var.set("Ready — waiting for a dubbing engine to be connected.")
            return

        self.is_running = True
        self.generate_button.configure(state="disabled", text="Generating…")
        self.progress_var.set(0)
        self.percent_label.configure(text="0%")
        self.status_var.set("Starting dubbing engine…")
        voice = VOICE_CHOICES.get(self.voice_var.get(), "hi-IN-SwaraNeural")
        thread = threading.Thread(
            target=self._run_engine,
            args=(engine, url, subtitle, output, voice),
            daemon=True
        )
        thread.start()

    def _run_engine(self, engine, url, subtitle, output, voice):
        # Expected engine interface:
        # python dubbing_engine.py --url URL --subtitles PATH_OR_URL --output FILE --voice VOICE
        try:
            command = [
                sys.executable, str(engine),
                "--url", url,
                "--subtitles", subtitle,
                "--output", output,
                "--voice", voice,
            ]
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace"
            )
            recent = []
            for line in process.stdout:
                line = line.strip()
                if line:
                    if not line.startswith("PROGRESS"):
                        recent = (recent + [line])[-12:]
                    self.root.after(0, self._handle_engine_line, line)
            code = process.wait()
            if code != 0:
                reason = next((l[6:].strip() for l in reversed(recent) if l.startswith("ERROR:")), None)
                if not reason:
                    reason = "\n".join(recent[-6:]) or "The engine printed no details."
                raise RuntimeError(f"Dubbing engine exited with code {code}.\n\n{reason}")
            self.root.after(0, self._generation_finished, output)
        except Exception as exc:
            self.root.after(0, self._generation_failed, str(exc))

    def _handle_engine_line(self, line):
        # Optional engine log protocol: "PROGRESS 35 message"
        match = re.match(r"PROGRESS\s+(\d{1,3})\s*(.*)", line)
        if match:
            value = min(100, max(0, int(match.group(1))))
            self.progress_var.set(value)
            self.percent_label.configure(text=f"{value}%")
            self.status_var.set(match.group(2) or "Working…")
        else:
            self.status_var.set(line[:160])

    def _generation_finished(self, output):
        self.is_running = False
        self.generate_button.configure(state="normal", text="Generate dubbed video")
        self.progress_var.set(100)
        self.percent_label.configure(text="100%")
        self.status_var.set("Finished successfully.")
        self.output_file = Path(output)
        self._sync_output_controls()
        messagebox.showinfo("Dubbing complete", f"Generated video:\n{output}")

    def _generation_failed(self, error):
        self.is_running = False
        self.generate_button.configure(state="normal", text="Generate dubbed video")
        self.status_var.set("Generation failed. See the error message.")
        # Provide user-friendly guidance based on error content
        err_lower = (error or "").lower()
        guidance = "\n\nSuggested fixes:\n"
        if "no audio" in err_lower or "no sound" in err_lower:
            guidance += "• Check that your subtitle file has proper timestamps and isn't too long per line.\n"
            guidance += "• Try a different subtitle source (uploaded vs auto-generated).\n"
        if "429" in err_lower:
            guidance += "• YouTube rate limit hit. Wait a few minutes and try again.\n"
        if "code 1" in err_lower:
            guidance += "• The dubbing engine encountered an issue. See console for details."
        messagebox.showerror("Dubbing failed", f"{error}\n{guidance}")

    def _sync_output_controls(self):
        path = Path(self.output_var.get()).expanduser()
        exists = path.is_file()
        state = "normal" if exists else "disabled"
        self.open_video_button.configure(state=state)
        self.open_folder_button.configure(state="normal" if path.parent.exists() else "disabled")

    def open_video(self):
        path = Path(self.output_var.get()).expanduser()
        if path.is_file():
            os.startfile(str(path))
        else:
            messagebox.showwarning("Video not found", "The output video has not been generated yet.")

    def open_output_folder(self):
        path = Path(self.output_var.get()).expanduser().parent
        if path.exists():
            os.startfile(str(path))
        else:
            messagebox.showwarning("Folder not found", "The selected output folder does not exist yet.")

    def open_youtube(self):
        url = self.youtube_var.get().strip()
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("YouTube URL", "Enter a YouTube URL first.")


def main():
    root = tk.Tk()
    try:
        root.iconname(APP_TITLE)
    except tk.TclError:
        pass
    DubbingApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()