from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pyttsx3
import pypdf
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploaded"
PROCESSED_DIR = DATA_DIR / "processed"
AUDIO_DIR = BASE_DIR / "static" / "audio_exports"

app = Flask(__name__)
app.secret_key = "math-a11y-demo"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

if not UPLOAD_DIR.exists():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
if not AUDIO_DIR.exists():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

app.sample_books: List[Dict[str, Any]] = []
app.current_book: Dict[str, Any] | None = None


def clean_text(text: str) -> str:
    text = text.replace("\u3000", " ")
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def split_segments(text: str, max_len: int = 180) -> List[str]:
    if not text:
        return []
    parts = re.split(r"(?<=[。！？；.])\s*", text)
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return [text[:max_len]]
    segments: List[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}{part}" if not current else f"{current} {part}"
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                segments.append(current)
            current = part
    if current:
        segments.append(current)
    return segments[:8]


def extract_block_text(block: Dict[str, Any]) -> str:
    content = block.get("content", {})
    if isinstance(content, dict):
        for key in ["title_content", "paragraph_content", "page_header_content", "content", "text"]:
            value = content.get(key)
            if isinstance(value, list):
                texts: List[str] = []
                for item in value:
                    if isinstance(item, dict):
                        inner = item.get("content")
                        if isinstance(inner, str):
                            texts.append(inner)
                        elif isinstance(inner, list):
                            for sub in inner:
                                if isinstance(sub, dict) and isinstance(sub.get("content"), str):
                                    texts.append(sub["content"])
                    elif isinstance(item, str):
                        texts.append(item)
                if texts:
                    return " ".join(texts)
            elif isinstance(value, str):
                return value
    return ""


def load_sample_books() -> List[Dict[str, Any]]:
    books: List[Dict[str, Any]] = []
    if not PROCESSED_DIR.exists():
        return books
    for child in sorted(PROCESSED_DIR.iterdir()):
        if not child.is_dir():
            continue
        meta_file = child / "metadata" / "pages.json"
        if not meta_file.exists():
            continue
        pages: List[Dict[str, Any]] = []
        raw_dir = child / "raw_mineru_output"
        page_dirs = sorted(raw_dir.glob("page_*"))
        for page_dir in page_dirs:
            md_file = page_dir / "auto" / f"{page_dir.name}.md"
            layout_file = child / "layout" / f"{page_dir.name}_layout.json"
            image_file = child / "pages_original" / f"{page_dir.name}.png"
            raw_text = md_file.read_text(encoding="utf-8", errors="ignore") if md_file.exists() else ""
            cleaned = clean_text(raw_text)
            blocks: List[Dict[str, Any]] = []
            if layout_file.exists():
                layout_data = json.loads(layout_file.read_text(encoding="utf-8", errors="ignore"))
                original_width = layout_data.get("original_image_width", 2174)
                original_height = layout_data.get("original_image_height", 3071)
                for block in layout_data.get("blocks", []):
                    text = extract_block_text(block)
                    if not text:
                        continue
                    bbox = block.get("original_image_bbox") or block.get("bbox") or []
                    if len(bbox) == 4:
                        x0, y0, x1, y1 = bbox
                        blocks.append({
                            "text": text,
                            "left_pct": round((x0 / original_width) * 100, 2),
                            "top_pct": round((y0 / original_height) * 100, 2),
                            "width_pct": round(((x1 - x0) / original_width) * 100, 2),
                            "height_pct": round(((y1 - y0) / original_height) * 100, 2),
                        })
            page_text = " ".join(block["text"] for block in blocks if block.get("text")) or cleaned
            image_url = None
            image_ratio = 0.7071
            if image_file.exists():
                image_url = f"/content/{image_file.relative_to(BASE_DIR).as_posix()}"
            if layout_file.exists():
                image_ratio = round(original_width / original_height, 6) if original_height else 0.7071
            pages.append({
                "page_id": page_dir.name.replace("page_", ""),
                "label": page_dir.name.replace("page_", ""),
                "text": page_text or "当前页面暂无可朗读文本",
                "segments": split_segments(page_text or "当前页面暂无可朗读文本"),
                "image_url": image_url,
                "image_ratio": image_ratio,
                "blocks": blocks,
            })
        if pages:
            books.append({
                "id": child.name,
                "title": child.name,
                "source": "processed",
                "pages": pages,
            })
    return books


def extract_pdf_pages(pdf_path: Path) -> List[Dict[str, Any]]:
    reader = pypdf.PdfReader(str(pdf_path))
    pages: List[Dict[str, Any]] = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        cleaned = clean_text(text)
        if cleaned:
            pages.append({
                "page_id": index,
                "label": str(index),
                "text": cleaned,
                "segments": split_segments(cleaned),
                "image_url": None,
                "image_ratio": 0.7071,
                "blocks": [],
            })
    if not pages:
        pages.append({
            "page_id": 1,
            "label": "1",
            "text": "上传的 PDF 未提取到可朗读文本，请尝试上传包含文字的教材文件。",
            "segments": ["上传的 PDF 未提取到可朗读文本，请尝试上传包含文字的教材文件。"],
            "image_url": None,
            "blocks": [],
        })
    return pages


def build_book_from_upload(filename: str, pdf_path: Path) -> Dict[str, Any]:
    pages = extract_pdf_pages(pdf_path)
    return {
        "id": f"upload_{uuid.uuid4().hex[:8]}",
        "title": filename,
        "source": "uploaded",
        "pages": pages,
    }


def speak_text(text: str, rate: int = 180) -> Dict[str, Any]:
    if not text.strip():
        return {"success": False, "message": "没有可朗读内容"}
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", int(rate))
        voices = engine.getProperty("voices")
        for voice in voices:
            name = getattr(voice, "name", "")
            if any(keyword in name for keyword in ["Chinese", "Mandarin", "Yunxi", "Huihui"]):
                engine.setProperty("voice", voice.id)
                break
        engine.say(text)
        engine.runAndWait()
        return {"success": True}
    except Exception as exc:  # pragma: no cover - depends on system TTS availability
        return {"success": False, "message": str(exc)}


def export_audio(text: str, rate: int = 180, filename: str | None = None) -> Dict[str, Any]:
    if not text.strip():
        return {"success": False, "message": "没有可导出的内容"}
    safe_name = filename or f"export_{uuid.uuid4().hex[:8]}"
    output_path = AUDIO_DIR / f"{safe_name}.wav"
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", int(rate))
        voices = engine.getProperty("voices")
        for voice in voices:
            name = getattr(voice, "name", "")
            if any(keyword in name for keyword in ["Chinese", "Mandarin", "Yunxi", "Huihui"]):
                engine.setProperty("voice", voice.id)
                break
        engine.save_to_file(text, str(output_path))
        engine.runAndWait()
        if output_path.exists():
            return {"success": True, "filename": output_path.name, "url": f"/audio/{output_path.name}"}
        return {"success": False, "message": "导出失败，未生成音频文件"}
    except Exception as exc:  # pragma: no cover - depends on system TTS availability
        return {"success": False, "message": str(exc)}


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        uploaded_file = request.files.get("file")
        if uploaded_file and uploaded_file.filename:
            filename = secure_filename(uploaded_file.filename)
            save_path = UPLOAD_DIR / filename
            uploaded_file.save(save_path)
            book = build_book_from_upload(filename, save_path)
            if not app.sample_books:
                app.sample_books = load_sample_books()
            app.sample_books.append(book)
            app.current_book = book
            return redirect(url_for("book_reader", book_id=book["id"], page_num=1))
    return render_template("home.html")


@app.route("/books")
def books():
    if not app.sample_books:
        app.sample_books = load_sample_books()
    return render_template("books.html", books=app.sample_books)


@app.route("/books/<book_id>")
@app.route("/books/<book_id>/page/<int:page_num>")
def book_reader(book_id: str, page_num: int = 1):
    if not app.sample_books:
        app.sample_books = load_sample_books()
    book = next((item for item in app.sample_books if item["id"] == book_id), None)
    if not book:
        return redirect(url_for("books"))

    app.current_book = book
    total_pages = len(book.get("pages", []))
    if total_pages == 0:
        return render_template("reader.html", book=book, page=None, page_num=1, total_pages=0, prev_page=None, next_page=None)

    safe_page_num = max(1, min(page_num, total_pages))
    page = book["pages"][safe_page_num - 1]
    prev_page = safe_page_num - 1 if safe_page_num > 1 else None
    next_page = safe_page_num + 1 if safe_page_num < total_pages else None
    return render_template(
        "reader.html",
        book=book,
        page=page,
        page_num=safe_page_num,
        total_pages=total_pages,
        prev_page=prev_page,
        next_page=next_page,
        default_rate=180,
    )


@app.route("/api/speak", methods=["POST"])
def api_speak():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "")
    rate = int(payload.get("rate", 180))
    result = speak_text(text, rate)
    return jsonify(result)


@app.route("/api/export", methods=["POST"])
def api_export():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "")
    rate = int(payload.get("rate", 180))
    filename = payload.get("filename")
    result = export_audio(text, rate, filename)
    return jsonify(result)


@app.route("/audio/<path:filename>")
def audio_file(filename: str):
    return send_from_directory(str(AUDIO_DIR), filename, as_attachment=True)


@app.route("/content/<path:filename>")
def content_file(filename: str):
    return send_from_directory(str(BASE_DIR), filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
