# YouTube to MP4

Paste a YouTube link. The video is saved as an `.mp4` in `Downloads\MP4 Downloader`.

## Run

Needs Python 3.

```
git clone https://github.com/anjailo-me/mp4-downloader.git
cd mp4-downloader
python -m pip install -r requirements.txt
python server.py
```

On Windows you can also double-click `start.bat` (it installs packages, then opens the app).

Then open http://127.0.0.1:8791/

Works with `youtube.com`, Shorts, and `youtu.be` links. Quality can be Best, 1080p, 720p, 480p, or 360p.
