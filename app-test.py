import glob
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import uuid
from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}


def free_port(host, port):
    """Bắt buộc giải phóng port bằng cách kill tiến trình đang chiếm dụng (chạy trên Unix/Linux/macOS)."""
    try:
        # Tìm PID của tiến trình đang lắng nghe ở port
        cmd = ["lsof", "-t", f"-i:{port}"]
        output = subprocess.check_output(cmd, text=True).strip()
        if output:
            for pid in output.split():
                os.kill(int(pid), signal.SIGKILL)
            print(f"[INFO] Đã kill tiến trình kẹt port {port} (PID: {output})")
    except Exception:
        pass  # Bỏ qua nếu port không bị chiếm hoặc không có quyền


def setup_signal_handlers():
    """Bắt các tín hiệu dừng ứng dụng để đóng port an toàn."""

    def handle_exit(sig, frame):
        print("\n[INFO] Đang đóng ứng dụng và giải phóng tài nguyên...")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)  # Ctrl + C
    signal.signal(signal.SIGTERM, handle_exit)  # Tín hiệu kill từ hệ thống


def parse_ytdlp_json(stdout):
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise ValueError("yt-dlp returned no data")


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += [
            "-f",
            f"{format_id}+bestaudio/best",
            "--merge-output-format",
            "mp4",
        ]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        if title:
            safe_title = (
                "".join(c for c in title if c not in r'\/:*?"<>|')
                .strip()[:100]
                .strip()
            )
            job["filename"] = (
                f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
            )
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = parse_ytdlp_json(result.stdout)

        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (
                    best_by_height[height].get("tbr") or 0
                ):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--flat-playlist", "-J", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)
        entries = info.get("entries", [])
        urls = [entry.get("url") for entry in entries if entry.get("url")]
        return jsonify({"urls": urls})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(
        target=run_download, args=(job_id, url, format_choice, format_id)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(
        job["file"], as_attachment=True, download_name=job["filename"]
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")

    # 1. Đăng ký hàm xử lý tín hiệu thoát an toàn
    setup_signal_handlers()

    # 2. Giải phóng port bị chiếm dụng bởi tiến trình treo trước đó (nếu có)
    free_port(host, port)

    # 3. Cấu hình Socket cho phép tái sử dụng địa chỉ ngay lập tức
    from werkzeug.serving import run_simple

    # Bật SO_REUSEADDR trên socket level
    socket.socket().setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Chạy ứng dụng bằng werkzeug server tích hợp sẵn với cấu hình chuẩn
    run_simple(host, port, app, use_reloader=False, use_debugger=False)
