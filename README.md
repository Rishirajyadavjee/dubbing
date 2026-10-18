# Hindi Dubbing Studio — Windows desktop app

## Run

1. Install Python 3.10+ and FFmpeg (make sure `ffmpeg` and `ffprobe` are on PATH).
2. Open PowerShell in this folder.
3. Install packages:

   ```powershell
   python -m pip install -U yt-dlp edge-tts
   ```

4. Launch the app:

   ```powershell
   python app.py
   ```

## Caption workflow

1. Paste a YouTube URL.
2. Click **Check captions**. The app loads the subtitle tracks that YouTube makes available, including uploaded and auto-generated captions.
3. Select a language from the dropdown.
4. Click **Download caption** and choose where to save the `.srt` file. The saved path is automatically filled into the subtitle field below.
5. Select the Hindi voice and output MP4 path, then click **Generate Hindi-dubbed video**.

You can also enter a local `.srt`/`.vtt` path or a direct subtitle URL in the subtitle field.

## Notes

- YouTube may not expose every caption track for downloading. Captions that are only visible through a translated on-screen menu may not appear as downloadable tracks.
- FFmpeg and FFprobe must be installed and available on PATH.
- Process only videos and captions you have permission to download and adapt.
