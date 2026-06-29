import base64
import csv
from io import BytesIO
import json
import os
import re
import shutil
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from mimetypes import guess_type

from flask import Flask, Response, current_app, flash, has_request_context, redirect, render_template, request, send_from_directory, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from pdf2image import convert_from_path
from PIL import Image
from werkzeug.utils import secure_filename

try:
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
LEGACY_UPLOAD_DIR = Path(__file__).resolve().parent / "static" / "uploads"
DB_PATH = DATA_DIR / "receipts.sqlite3"
BACKUP_DIR = DATA_DIR / "backups"
SEED_CSV = Path(__file__).resolve().parent / "seed" / "renovation_costs.csv"

CATEGORIES = [
    "Projektiranje",
    "Gradbena dela",
    "Materiali",
    "Instalacije",
    "Pohištvo / oprema",
    "Ostalo",
]

OCR_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "pdf"}
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "128"))
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "local")
LLM_FALLBACK_BASE_URL = os.environ.get("LLM_FALLBACK_BASE_URL", "").rstrip("/")
LLM_FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", "")
LLM_FALLBACK_API_KEY = os.environ.get("LLM_FALLBACK_API_KEY", "local")
OPENROUTER_SITE_URL = os.environ.get("OPENROUTER_SITE_URL", "")
OPENROUTER_APP_NAME = os.environ.get("OPENROUTER_APP_NAME", "Vesel Salmič")
LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "45"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8192"))
LLM_OCR_CHARS = int(os.environ.get("LLM_OCR_CHARS", "14000"))
LLM_SEND_IMAGES = os.environ.get("LLM_SEND_IMAGES", "1").lower() in {"1", "true", "yes"}
LLM_IMAGE_MAX_FILES = int(os.environ.get("LLM_IMAGE_MAX_FILES", "6"))
LLM_IMAGE_MAX_PDF_PAGES = int(os.environ.get("LLM_IMAGE_MAX_PDF_PAGES", "3"))
LLM_IMAGE_MAX_SIDE = int(os.environ.get("LLM_IMAGE_MAX_SIDE", "1600"))
LLM_IMAGE_JPEG_QUALITY = int(os.environ.get("LLM_IMAGE_JPEG_QUALITY", "85"))
BACKUP_INTERVAL_HOURS = int(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_uploads()
    init_db()

    @app.before_request
    def automated_backup():
        maybe_run_backup()

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(error):
        flash(f"Upload is too large. Limit is {MAX_UPLOAD_MB} MB per request.")
        return redirect(request.referrer or url_for("new_receipt"))

    @app.get("/")
    def index():
        summary = get_summary()
        recent_receipts = list_receipts({}, limit=5)
        return render_template("index.html", summary=summary, categories=get_categories(), recent_receipts=recent_receipts)

    @app.get("/expenses")
    def expenses():
        filters = {
            "q": request.args.get("q", "").strip(),
            "category": request.args.get("category", "").strip(),
            "payer": request.args.get("payer", "").strip(),
            "vendor": request.args.get("vendor", "").strip(),
        }
        sort = request.args.get("sort", "date").strip()
        direction = request.args.get("direction", "desc").strip()
        receipts = list_receipts(filters, sort=sort, direction=direction)
        filtered_summary = get_receipt_totals(filters)
        sort_state = normalize_receipt_sort(sort, direction)
        return render_template(
            "expenses.html",
            receipts=receipts,
            categories=get_categories(),
            vendors=get_vendors(),
            filters=filters,
            filtered_summary=filtered_summary,
            sort_state=sort_state,
        )

    @app.get("/new")
    def new_receipt():
        draft = empty_draft()
        return render_template(
            "receipt_form.html",
            draft=draft,
            categories=get_categories(),
            vendors=get_vendors(),
            raw_text="",
            files=[],
            form_action=url_for("create_receipt"),
            page_title="Add receipt",
            submit_label="Save receipt",
            can_add_files=False,
        )

    @app.post("/scan")
    def scan_receipt():
        uploads = valid_uploads(request.files.getlist("receipt"))
        if not uploads:
            flash("Choose one or more files first.")
            return redirect(url_for("new_receipt"))

        files = [save_upload(upload) for upload in uploads]
        raw_text = ""
        draft = parse_receipt_text(raw_text, get_categories(), get_vendors(), files)
        if not draft_has_extracted_data(draft):
            flash("Model ni vrnil uporabnih podatkov iz slike. Preveri OpenRouter log/API ključ ali poskusi z jasnejšo sliko.")
        draft["file_names"] = files_to_json(files)
        return render_template(
            "receipt_form.html",
            draft=draft,
            categories=get_categories(),
            vendors=get_vendors(),
            raw_text=raw_text,
            files=files,
            form_action=url_for("create_receipt"),
            page_title="Review entry",
            submit_label="Save receipt",
            can_add_files=False,
        )

    @app.post("/receipts")
    def create_receipt():
        data = normalize_form(request.form)
        errors = validate_receipt(data)
        if errors:
            for error in errors:
                flash(error)
            return render_template(
                "receipt_form.html",
                draft=data,
                categories=get_categories(),
                vendors=get_vendors(),
                raw_text=request.form.get("ocr_text", ""),
                files=parse_files_json(data.get("file_names", "[]")),
                form_action=url_for("create_receipt"),
                page_title="Review entry",
                submit_label="Save receipt",
                can_add_files=False,
            )
        receipt_id = insert_receipt(data)
        insert_receipt_files(receipt_id, parse_files_json(data.get("file_names", "[]")))
        audit_log("create", receipt_id, {"description": data["description"], "total": money(data["total"])})
        flash("Receipt saved.")
        return redirect(url_for("expenses"))

    @app.get("/receipts/<int:receipt_id>/edit")
    def edit_receipt(receipt_id):
        receipt = get_receipt(receipt_id)
        if receipt is None:
            flash("Receipt not found.")
            return redirect(url_for("expenses"))
        draft = receipt_to_draft(receipt)
        files = get_receipt_files(receipt_id)
        return render_template(
            "receipt_form.html",
            draft=draft,
            categories=get_categories(),
            vendors=get_vendors(),
            raw_text=draft.get("ocr_text", ""),
            files=files,
            audit_logs=get_audit_logs(receipt_id),
            form_action=url_for("update_receipt", receipt_id=receipt_id),
            page_title="Edit receipt",
            submit_label="Update receipt",
            can_add_files=True,
        )

    @app.get("/receipts/<int:receipt_id>")
    def receipt_detail(receipt_id):
        receipt = get_receipt(receipt_id)
        if receipt is None:
            flash("Receipt not found.")
            return redirect(url_for("expenses"))
        receipt = dict(receipt)
        receipt["files"] = get_receipt_files(receipt_id)
        return render_template(
            "receipt_detail.html",
            receipt=receipt,
            audit_logs=get_audit_logs(receipt_id),
            validation_status=get_latest_validation_status(receipt_id),
        )

    @app.post("/receipts/<int:receipt_id>")
    def update_receipt(receipt_id):
        receipt = get_receipt(receipt_id)
        if receipt is None:
            flash("Receipt not found.")
            return redirect(url_for("expenses"))

        form = request.form.copy()
        uploads = valid_uploads(request.files.getlist("attachments"))

        data = normalize_form(form)
        errors = validate_receipt(data)
        if errors:
            for error in errors:
                flash(error)
            return render_template(
                "receipt_form.html",
                draft=data,
                categories=get_categories(),
                vendors=get_vendors(),
                raw_text=form.get("ocr_text", ""),
                files=get_receipt_files(receipt_id),
                audit_logs=get_audit_logs(receipt_id),
                form_action=url_for("update_receipt", receipt_id=receipt_id),
                page_title="Edit receipt",
                submit_label="Update receipt",
                can_add_files=True,
            )
        new_files = [save_upload(upload) for upload in uploads]
        before = receipt_to_audit_dict(receipt)
        update_receipt_by_id(receipt_id, data)
        insert_receipt_files(receipt_id, new_files)
        audit_log("update", receipt_id, {"before": before, "after": receipt_data_to_audit_dict(data), "new_files": len(new_files)})
        flash("Receipt updated.")
        return redirect(url_for("expenses"))

    @app.post("/receipts/<int:receipt_id>/files/<path:file_name>/delete")
    def delete_receipt_file(receipt_id, file_name):
        if get_receipt(receipt_id) is None:
            flash("Receipt not found.")
            return redirect(url_for("expenses"))
        delete_receipt_file_by_name(receipt_id, file_name)
        audit_log("delete_file", receipt_id, {"file_name": file_name})
        flash("Attachment deleted.")
        return redirect(url_for("edit_receipt", receipt_id=receipt_id))

    @app.post("/receipts/<int:receipt_id>/delete")
    def delete_receipt(receipt_id):
        if get_receipt(receipt_id) is None:
            flash("Receipt not found.")
            return redirect(url_for("expenses"))
        delete_receipt_by_id(receipt_id)
        audit_log("delete", receipt_id, {})
        flash("Receipt moved to trash.")
        return redirect(url_for("expenses"))

    @app.post("/receipts/<int:receipt_id>/validate")
    def validate_receipt_with_llm(receipt_id):
        receipt = get_receipt(receipt_id)
        if receipt is None:
            flash("Receipt not found.")
            return redirect(url_for("expenses"))

        result = validate_saved_receipt_with_llm(receipt, get_receipt_files(receipt_id))
        if result is None:
            audit_log("validate_failed", receipt_id, {})
            flash("LLM validation failed.")
            return redirect(request.referrer or url_for("expenses"))

        audit_log("validate", receipt_id, result)
        status = clean_model_string(result.get("status")) or "checked"
        issues = result.get("issues") if isinstance(result.get("issues"), list) else []
        if issues:
            flash(f"Validation {status}: {issues[0]}")
        else:
            flash(f"Validation {status}: no obvious issues.")
        return redirect(request.referrer or url_for("expenses"))

    @app.get("/trash")
    def trash():
        receipts = list_deleted_receipts()
        return render_template("trash.html", receipts=receipts)

    @app.get("/backups")
    def backups():
        return render_template(
            "backups.html",
            backups=list_backup_files(),
            last_backup_at=get_app_meta("last_backup_at"),
            backup_interval_hours=BACKUP_INTERVAL_HOURS,
            backup_retention_days=BACKUP_RETENTION_DAYS,
        )

    @app.post("/backups/run")
    def run_backup():
        maybe_run_backup(force=True)
        flash("Backup created.")
        return redirect(url_for("backups"))

    @app.get("/database/download")
    def download_database():
        return send_from_directory(DATA_DIR, DB_PATH.name, as_attachment=True, download_name="receipts-current.sqlite3")

    @app.get("/backups/<path:file_name>")
    def download_backup(file_name):
        safe_name = Path(file_name).name
        if not safe_name or safe_name != file_name:
            flash("Backup not found.")
            return redirect(url_for("backups"))
        return send_from_directory(BACKUP_DIR, safe_name, as_attachment=True)

    @app.post("/receipts/<int:receipt_id>/restore")
    def restore_receipt(receipt_id):
        if restore_receipt_by_id(receipt_id):
            audit_log("restore", receipt_id, {})
            flash("Receipt restored.")
        else:
            flash("Receipt not found in trash.")
        return redirect(url_for("trash"))

    @app.get("/export.csv")
    def export_csv():
        rows = list_receipts({})

        def generate():
            output = []
            writer = csv.writer(output := CsvBuffer())
            writer.writerow([
                "#",
                "Opis stroška / Storitev",
                "Kategorija",
                "Datum",
                "Dobavitelj / Izvajalec",
                "Račun / Ref.",
                "Skupna cena z DDV (€)",
                "Plačala Ana (€)",
                "Plačal Jakob (€)",
                "Opombe",
            ])
            yield output.flush()
            for i, row in enumerate(rows, start=1):
                writer.writerow([
                    i,
                    row["description"],
                    row["category"],
                    format_date(row["date"]),
                    row["vendor"],
                    row["reference"],
                    money(row["total"]),
                    money(row["ana_paid"]),
                    money(row["jakob_paid"]),
                    row["notes"],
                ])
                yield output.flush()

        return Response(generate(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=renovation_receipts.csv"})

    @app.get("/uploads/<path:file_name>")
    def uploaded_file(file_name):
        stored_file = get_file_by_name(file_name)
        if stored_file and stored_file.get("content") is not None:
            return Response(
                stored_file["content"],
                mimetype=stored_file.get("content_type") or "application/octet-stream",
                headers={"Content-Disposition": content_disposition("inline", stored_file.get("original_name") or file_name)},
            )
        return send_from_directory(UPLOAD_DIR, file_name)

    @app.context_processor
    def template_helpers():
        return {
            "file_label": file_label,
            "is_image_file": is_image_file,
            "primary_preview_file": primary_preview_file,
            "format_date": format_date,
            "iso_date": iso_date,
            "format_datetime": format_datetime,
            "format_file_size": format_file_size,
            "expense_sort_url": expense_sort_url,
        }

    return app


class CsvBuffer:
    def __init__(self):
        self.parts = []

    def write(self, value):
        self.parts.append(value)

    def flush(self):
        value = "".join(self.parts)
        self.parts = []
        return value


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def maybe_run_backup(force=False):
    if BACKUP_INTERVAL_HOURS <= 0:
        return
    now = datetime.utcnow()
    with get_db() as db:
        row = db.execute("SELECT value FROM app_meta WHERE key = 'last_backup_at'").fetchone()
        last_backup = parse_iso_datetime(row["value"]) if row else None
        if not force and last_backup and now - last_backup < timedelta(hours=BACKUP_INTERVAL_HOURS):
            return
        backup_name = f"receipts-{now:%Y%m%d-%H%M%S}.sqlite3"
        backup_path = BACKUP_DIR / backup_name
        backup_database(backup_path)
        db.execute(
            "INSERT INTO app_meta (key, value) VALUES ('last_backup_at', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (now.isoformat(timespec="seconds"),),
        )
        db.execute(
            "INSERT INTO audit_log (receipt_id, action, details, created_at) VALUES (NULL, ?, ?, ?)",
            ("backup", json.dumps({"file": str(backup_path.relative_to(DATA_DIR))}), now.isoformat(timespec="seconds")),
        )
    prune_backups()
    current_app.logger.info("SQLite backup created: %s", backup_path)


def backup_database(backup_path):
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(DB_PATH)
    try:
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def prune_backups():
    if BACKUP_RETENTION_DAYS <= 0:
        return
    cutoff = datetime.utcnow() - timedelta(days=BACKUP_RETENTION_DAYS)
    for path in BACKUP_DIR.glob("receipts-*.sqlite3"):
        try:
            created = datetime.utcfromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if created < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def get_app_meta(key):
    with get_db() as db:
        row = db.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def list_backup_files():
    backups = []
    paths = []
    for path in BACKUP_DIR.glob("receipts-*.sqlite3"):
        try:
            paths.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(paths, key=lambda item: item[0], reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        backups.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
        )
    return backups


def format_datetime(value):
    parsed = parse_iso_datetime(value)
    if parsed is None:
        return value or ""
    return parsed.strftime("%d.%m.%Y %H:%M")


def format_file_size(size):
    size = int(size or 0)
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024


def init_db():
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                category TEXT NOT NULL,
                date TEXT,
                vendor TEXT,
                reference TEXT,
                total REAL NOT NULL,
                ana_paid REAL NOT NULL DEFAULT 0,
                jakob_paid REAL NOT NULL DEFAULT 0,
                notes TEXT,
                file_name TEXT,
                ocr_text TEXT,
                source_key TEXT,
                deleted_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        columns = [row["name"] for row in db.execute("PRAGMA table_info(receipts)").fetchall()]
        if "source_key" not in columns:
            db.execute("ALTER TABLE receipts ADD COLUMN source_key TEXT")
        if "deleted_at" not in columns:
            db.execute("ALTER TABLE receipts ADD COLUMN deleted_at TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_source_key ON receipts(source_key)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS receipt_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id INTEGER,
                file_name TEXT NOT NULL,
                original_name TEXT,
                content_type TEXT,
                content BLOB,
                upload_token TEXT,
                deleted_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
            )
            """
        )
        receipt_file_columns_info = db.execute("PRAGMA table_info(receipt_files)").fetchall()
        receipt_id_info = next((row for row in receipt_file_columns_info if row["name"] == "receipt_id"), None)
        if receipt_id_info and receipt_id_info["notnull"]:
            db.execute("ALTER TABLE receipt_files RENAME TO receipt_files_old")
            db.execute(
                """
                CREATE TABLE receipt_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id INTEGER,
                    file_name TEXT NOT NULL,
                    original_name TEXT,
                    content_type TEXT,
                    content BLOB,
                    upload_token TEXT,
                    deleted_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
                )
                """
            )
            old_columns = [row["name"] for row in db.execute("PRAGMA table_info(receipt_files_old)").fetchall()]
            content_type_expr = "content_type" if "content_type" in old_columns else "NULL"
            content_expr = "content" if "content" in old_columns else "NULL"
            upload_token_expr = "upload_token" if "upload_token" in old_columns else "NULL"
            deleted_at_expr = "deleted_at" if "deleted_at" in old_columns else "NULL"
            db.execute(
                f"""
                INSERT INTO receipt_files (
                    id, receipt_id, file_name, original_name, content_type, content, upload_token, deleted_at, created_at
                )
                SELECT id, receipt_id, file_name, original_name, {content_type_expr}, {content_expr}, {upload_token_expr}, {deleted_at_expr}, created_at
                FROM receipt_files_old
                """
            )
            db.execute("DROP TABLE receipt_files_old")
        receipt_file_columns = [row["name"] for row in db.execute("PRAGMA table_info(receipt_files)").fetchall()]
        if "content_type" not in receipt_file_columns:
            db.execute("ALTER TABLE receipt_files ADD COLUMN content_type TEXT")
        if "content" not in receipt_file_columns:
            db.execute("ALTER TABLE receipt_files ADD COLUMN content BLOB")
        if "upload_token" not in receipt_file_columns:
            db.execute("ALTER TABLE receipt_files ADD COLUMN upload_token TEXT")
        if "deleted_at" not in receipt_file_columns:
            db.execute("ALTER TABLE receipt_files ADD COLUMN deleted_at TEXT")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_receipts_category ON receipts(category)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_receipts_date ON receipts(date)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_receipts_vendor ON receipts(vendor)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_receipts_deleted_at ON receipts(deleted_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_receipt_files_receipt_id ON receipt_files(receipt_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_receipt_files_upload_token ON receipt_files(upload_token)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_receipt_id ON audit_log(receipt_id)")
        migrate_receipt_files(db)
        seed_receipts(db)


def migrate_receipt_files(db):
    rows = db.execute(
        """
        SELECT id, file_name
        FROM receipts
        WHERE TRIM(COALESCE(file_name, '')) != ''
        """
    ).fetchall()
    for row in rows:
        exists = db.execute(
            "SELECT 1 FROM receipt_files WHERE receipt_id = ? AND file_name = ?",
            (row["id"], row["file_name"]),
        ).fetchone()
        if exists:
            continue
        db.execute(
            "INSERT INTO receipt_files (receipt_id, file_name, original_name, created_at) VALUES (?, ?, ?, ?)",
            (row["id"], row["file_name"], row["file_name"], datetime.utcnow().isoformat(timespec="seconds")),
        )
    file_rows = db.execute(
        """
        SELECT id, file_name, original_name
        FROM receipt_files
        WHERE content IS NULL OR length(content) = 0
        """
    ).fetchall()
    for row in file_rows:
        file_name = Path(row["file_name"] or "").name
        if not file_name or file_name != row["file_name"]:
            continue
        path = UPLOAD_DIR / file_name
        if not path.exists():
            continue
        content_type = guess_type(row["original_name"] or file_name)[0] or "application/octet-stream"
        db.execute(
            "UPDATE receipt_files SET content = ?, content_type = ? WHERE id = ?",
            (sqlite3.Binary(path.read_bytes()), content_type, row["id"]),
        )


def migrate_legacy_uploads():
    if not LEGACY_UPLOAD_DIR.exists():
        return
    for path in LEGACY_UPLOAD_DIR.iterdir():
        if not path.is_file() or path.name == ".gitkeep":
            continue
        target = UPLOAD_DIR / path.name
        if not target.exists():
            shutil.copy2(path, target)


def seed_receipts(db):
    if not SEED_CSV.exists():
        return
    with SEED_CSV.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            source_key = row.get("source_key", "").strip()
            if not source_key:
                continue
            if db.execute("SELECT 1 FROM receipts WHERE source_key = ?", (source_key,)).fetchone():
                continue
            ana = parse_money(row.get("ana_paid")) or Decimal("0")
            jakob = parse_money(row.get("jakob_paid")) or Decimal("0")
            total = parse_money(row.get("total")) or ana + jakob
            db.execute(
                """
                INSERT INTO receipts (
                    description, category, date, vendor, reference, total, ana_paid,
                    jakob_paid, notes, file_name, ocr_text, source_key, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("description", "").strip(),
                    normalize_category(row.get("category")),
                    row.get("date", "").strip(),
                    row.get("vendor", "").strip(),
                    row.get("reference", "").strip(),
                    float(total),
                    float(ana),
                    float(jakob),
                    row.get("notes", "").strip(),
                    "",
                    "",
                    source_key,
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            )


def empty_draft():
    return {
        "description": "",
        "category": "Materiali",
        "date": "",
        "vendor": "",
        "reference": "",
        "total": "",
        "ana_paid": "",
        "jakob_paid": "",
        "notes": "",
        "file_name": "",
        "file_names": "[]",
        "ocr_text": "",
    }


def receipt_to_draft(receipt):
    return {
        "description": receipt["description"] or "",
        "category": receipt["category"] or "Materiali",
        "date": format_date(receipt["date"]),
        "vendor": receipt["vendor"] or "",
        "reference": receipt["reference"] or "",
        "total": money(receipt["total"]),
        "ana_paid": money(receipt["ana_paid"]),
        "jakob_paid": money(receipt["jakob_paid"]),
        "notes": receipt["notes"] or "",
        "file_name": receipt["file_name"] or "",
        "file_names": files_to_json(get_receipt_files(receipt["id"])),
        "ocr_text": receipt["ocr_text"] or "",
    }


def valid_uploads(uploads):
    return [upload for upload in uploads if upload and upload.filename]


def is_image_file(filename):
    return (filename or "").rsplit(".", 1)[-1].lower() in {"png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp"}


def is_ocr_file(filename):
    return "." in (filename or "") and filename.rsplit(".", 1)[1].lower() in OCR_EXTENSIONS


def file_label(filename):
    if "." not in (filename or ""):
        return "FILE"
    return filename.rsplit(".", 1)[1].upper()


def primary_preview_file(files):
    for file in files or []:
        if is_image_file(file.get("file_name", "")):
            return file
    return files[0] if files else None


def save_upload(upload):
    original = secure_filename(upload.filename) or f"upload-{uuid.uuid4().hex[:8]}"
    if "." in original:
        ext = "." + original.rsplit(".", 1)[1].lower()
    else:
        ext = ""
    stored_name = f"{uuid.uuid4().hex}{ext}"
    content = upload.read()
    content_type = upload.mimetype or guess_type(original)[0] or "application/octet-stream"
    upload_token = uuid.uuid4().hex
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO receipt_files (
                receipt_id, file_name, original_name, content_type, content, upload_token, created_at
            )
            VALUES (NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored_name,
                original,
                content_type,
                sqlite3.Binary(content),
                upload_token,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
    return {
        "id": cursor.lastrowid,
        "file_name": stored_name,
        "original_name": original,
        "upload_token": upload_token,
        "content_type": content_type,
    }


def run_ocr(path):
    if pytesseract is None:
        return ""

    try:
        if path.suffix.lower() == ".pdf":
            pages = convert_from_path(path, dpi=220, first_page=1, last_page=3)
            return "\n\n".join(pytesseract.image_to_string(page, lang="slv+eng") for page in pages)
        with Image.open(path) as image:
            image = image.convert("RGB")
            return pytesseract.image_to_string(image, lang="slv+eng")
    except Exception as exc:
        return f"OCR failed: {exc}"


def run_ocr_for_files(files):
    chunks = []
    for file in files:
        file_name = file.get("file_name", "")
        if not is_ocr_file(file_name):
            continue
        text = run_ocr(UPLOAD_DIR / file_name).strip()
        if text:
            chunks.append(f"--- {file.get('original_name', file_name)} ---\n{text}")
    return "\n\n".join(chunks)


def parse_receipt_text(text, categories=None, vendors=None, files=None):
    llm_draft = parse_receipt_text_with_llm(text, categories or [], vendors or [], files or [])
    if llm_draft:
        return llm_draft

    if not text.strip():
        draft = empty_draft()
        draft["ocr_text"] = text
        return draft

    draft = empty_draft()
    lines = [clean_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    vendor = first_vendor_line(lines)
    total = extract_total(text)
    date = extract_date(text)
    reference = prefer_main_receipt_reference(text, extract_reference(text))

    draft.update(
        {
            "vendor": vendor,
            "description": default_description(text),
            "date": format_date(date),
            "reference": reference,
            "total": money(total) if total is not None else "",
            "category": suggest_category(text),
            "notes": extract_notes(text),
            "ocr_text": text,
        }
    )
    return draft


def draft_has_extracted_data(draft):
    return any((draft.get("vendor"), draft.get("reference"), draft.get("total"), draft.get("date"), draft.get("notes")))


def parse_receipt_text_with_llm(text, categories, vendors, files=None):
    files = files or []
    providers = llm_providers()
    if not providers:
        return None

    image_urls = prepare_images_for_llm(files)
    if not text.strip() and not image_urls:
        return None

    llm_text = prepare_ocr_for_llm(text)

    parsed = None
    for provider in providers:
        payloads = [build_llm_payload(llm_text, categories, vendors, provider["model"], image_urls)]
        if image_urls and llm_text.strip():
            payloads.append(build_llm_payload(llm_text, categories, vendors, provider["model"], []))

        for payload in payloads:
            has_images = user_message_has_images(payload)
            try:
                content = call_llm_chat(payload, provider)
                log_llm_output(content, has_images, provider)
                parsed = extract_json_object(content)
                log_llm_parsed(parsed, has_images, provider)
                break
            except (OSError, URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
                log_llm_error(exc, has_images, provider)
                parsed = None
        if parsed is not None:
            break

    if parsed is None:
        return None

    draft = empty_draft()
    payment_total = extract_received_payment_total(text)
    total = payment_total or parse_model_money(parsed.get("total"))
    ana_paid = parse_model_money(parsed.get("ana_paid"))
    jakob_paid = parse_model_money(parsed.get("jakob_paid"))
    vendor = clean_model_vendor(parsed.get("vendor"))
    description = strip_vendor_from_description(
        clean_model_string(parsed.get("description")) or default_description(text),
        vendor,
    )
    reference = prefer_main_receipt_reference(text, clean_model_string(parsed.get("reference")))
    if is_prepayment_only_total(text, total):
        total = None
    draft.update(
        {
            "description": description,
            "category": normalize_category(correct_category(text, clean_model_string(parsed.get("category")) or suggest_category(text))),
            "date": format_date(normalize_date(clean_model_string(parsed.get("date")) or "")),
            "vendor": vendor or first_vendor_line([clean_line(line) for line in text.splitlines() if clean_line(line)]),
            "reference": reference,
            "total": money(total) if total is not None else "",
            "ana_paid": money(ana_paid) if ana_paid is not None else "",
            "jakob_paid": money(jakob_paid) if jakob_paid is not None else "",
            "notes": merge_item_notes(clean_model_string(parsed.get("notes")), extract_notes(text)),
            "ocr_text": text,
        }
    )
    return draft


def build_llm_payload(llm_text, categories, vendors, model, image_urls=None):
    # Ideally pre-filter vendors from OCR instead of passing first 60 blindly
    vendor_candidates = vendors[:30]
    category_candidates = categories[:40]
    ocr_section = f"\n\nOCR:\n{llm_text[:LLM_OCR_CHARS]}" if llm_text.strip() else ""

    prompt = f"""
You are reading receipt images from one Slovenian/English renovation receipt or invoice.
There may be multiple photos/pages of the same document.
Use the images to read layout, totals, receipt numbers, dates, vendor, and purchased products.

Task:
Return one compact valid JSON object with exactly these keys:
{{
  "description": null,
  "category": null,
  "date": null,
  "vendor": null,
  "reference": null,
  "total": null,
  "ana_paid": null,
  "jakob_paid": null,
  "notes": null
}}

General rules:
- Output JSON only. No markdown and no explanation.
- Unknown or uncertain value = null.
- Do not invent values that are not supported by the receipt image.
- Merge pages into one receipt entry.
- Before writing notes, mentally group rows that describe the same physical item.
- Write description and notes in Slovenian when possible.
- If There are multiple pages make sure to list all items across pages.

Field rules:
- date = YYYY-MM-DD.
- total, ana_paid, jakob_paid = numbers, not strings.
- ana_paid and jakob_paid are null unless OCR explicitly says who paid.
- description = short Slovenian renovation purchase summary, max 8 words, no vendor name.
- category = best match from this list if possible: {json.dumps(category_candidates, ensure_ascii=False)}
- vendor = actual seller/company from OCR or image, preserving the real vendor name.
- vendor should be a clean display name without legal suffixes such as "d.o.o.", "s.p.", "d.d.", "Ltd", "LLC", or similar.
- Existing vendor names are spelling hints only, not a closed list: {json.dumps(vendor_candidates, ensure_ascii=False)}
- If the seller is not in the existing list, return the new seller anyway. For example, if the receipt says "Digiklik", return "Digiklik".
- Do not return the customer/buyer, delivery address, payer, bank, payment card, or tax authority as vendor.
- reference = invoice/receipt number from "Številka", "Stevilka", "Račun", or a clearly labeled receipt/invoice number.
- reference must not be POSEL, Veza, customer number, tax number, BIZ/loyalty card number, phone number, or VAT ID.

Amount rules:
- total = final amount paid / payable including DDV/VAT.
- Prefer "Prejeta plačila", "Skupaj plačila", card payment, cash payment, or the final "Skupaj" near payment lines.
- Do not use "Davčna osnova", "Vrednost brez DDV", subtotal/tax-base lines, loyalty points, or VAT amounts as total.
- For Merkur receipts, prefer "Prejeta plačila" or "Skupaj plačila" over earlier "Skupaj".

Prepayment rules:
- If both "RAČUN" and "RAČUN za PREDPLAČILO" appear, use the main "RAČUN".
- Do not use prepayment receipt number, items, or amount as main reference/description/total.
- If only a prepayment receipt amount is visible, set total = null.
- Do not mention prepayment in notes.

Notes:
- notes must be one JSON string.
- Use newline-separated "- " bullets.
- notes must contain ONLY the purchased product/service list.
- Do not include payment, card type, total, invoice/reference number, prepayment, date, vendor, customer, address, tax, discount, warranty, legal text, or generic filler in notes.
- Group repeated OCR lines, barcode lines, product-code lines, discount lines, and wrapped continuation lines into one bullet per real item.
- Do not create separate bullets for barcode/product code, quantity, unit, discount, tax, or wrapped text if they belong to the same item.
- Prefer 2-5 concise item bullets, not every OCR line.
- If several rows are variants of the same product family, combine them into one useful summary.
- Think carefully before writing each bullet: if two detected rows refer to the same real item, merge them into one Slovenian product summary.
- Exclude warranty/legal text, tax IDs, addresses, loyalty points, customer data, OCR noise.
{ocr_section}
""".strip()

    user_content = [{"type": "text", "text": prompt}]
    for image_url in image_urls or []:
        user_content.append({"type": "image_url", "image_url": {"url": image_url}})

    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You extract receipt data. Output only valid JSON. No markdown. No explanation.",
            },
            {"role": "user", "content": user_content if image_urls else prompt},
        ],
        "max_tokens": LLM_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }


def validate_saved_receipt_with_llm(receipt, files):
    providers = llm_providers()
    if not providers:
        return None

    image_urls = prepare_images_for_llm(files)
    receipt_data = {
        "description": receipt["description"],
        "category": receipt["category"],
        "date": receipt["date"],
        "vendor": receipt["vendor"],
        "reference": receipt["reference"],
        "total": receipt["total"],
        "ana_paid": receipt["ana_paid"],
        "jakob_paid": receipt["jakob_paid"],
        "notes": receipt["notes"] or "",
    }

    for provider in providers:
        payload = build_validation_payload(receipt_data, provider["model"], image_urls)
        has_images = user_message_has_images(payload)
        try:
            content = call_llm_chat(payload, provider)
            log_llm_output(content, has_images, provider)
            parsed = extract_json_object(content)
            log_llm_parsed(parsed, has_images, provider)
            return parsed
        except (OSError, URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
            log_llm_error(exc, has_images, provider)
    return None


def build_validation_payload(receipt_data, model, image_urls=None):
    prompt = f"""
You are validating one saved renovation receipt entry against the attached receipt image(s).

Return one compact valid JSON object with exactly these keys:
{{
  "status": "ok",
  "issues": [],
  "suggestions": []
}}

Rules:
- Output JSON only. No markdown and no explanation.
- status must be "ok", "warning", or "error".
- issues: concrete possible mistakes in the saved entry.
- suggestions: concise suggested corrections, in Slovenian when possible.
- Check vendor, date, reference, total, category, description, and item notes.
- Do not complain about missing payer split unless the receipt explicitly says who paid.
- Do not require legal suffixes like d.o.o. or s.p. in vendor.
- Notes should contain only purchased product/service items, not payment/reference/legal text.

Saved entry:
{json.dumps(receipt_data, ensure_ascii=False, sort_keys=True)}
""".strip()

    user_content = [{"type": "text", "text": prompt}]
    for image_url in image_urls or []:
        user_content.append({"type": "image_url", "image_url": {"url": image_url}})

    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "You validate receipt data. Output only valid JSON. No markdown. No explanation."},
            {"role": "user", "content": user_content if image_urls else prompt},
        ],
        "max_tokens": min(LLM_MAX_TOKENS, 2048),
        "response_format": {"type": "json_object"},
    }


def prepare_images_for_llm(files):
    if not LLM_SEND_IMAGES:
        return []

    image_urls = []
    for file in files:
        if len(image_urls) >= LLM_IMAGE_MAX_FILES:
            break

        stored_file = get_pending_or_saved_file(file)
        if not stored_file or not stored_file.get("content"):
            continue

        try:
            file_name = stored_file.get("file_name", "")
            content = stored_file["content"]
            if file_name.lower().endswith(".pdf"):
                pdf_path = write_temp_llm_file(file_name, content)
                try:
                    pages = convert_from_path(pdf_path, dpi=180, first_page=1, last_page=LLM_IMAGE_MAX_PDF_PAGES)
                finally:
                    try:
                        pdf_path.unlink()
                    except OSError:
                        pass
                for page in pages:
                    if len(image_urls) >= LLM_IMAGE_MAX_FILES:
                        break
                    image_urls.append(image_to_data_url(page))
            elif is_image_file(file_name):
                with Image.open(BytesIO(content)) as image:
                    image_urls.append(image_to_data_url(image))
        except Exception as exc:
            current_app.logger.warning("Could not prepare image for LLM (%s): %s", file_name, exc)
            continue

    return image_urls


def image_to_data_url(image):
    image = image.convert("RGB")
    image.thumbnail((LLM_IMAGE_MAX_SIDE, LLM_IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=LLM_IMAGE_JPEG_QUALITY, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def write_temp_llm_file(file_name, content):
    temp_path = Path("/tmp") / f"receipt-llm-{uuid.uuid4().hex}{Path(file_name).suffix.lower()}"
    temp_path.write_bytes(content)
    return temp_path


def user_message_has_images(payload):
    messages = payload.get("messages") or []
    if not messages:
        return False
    content = messages[-1].get("content")
    return isinstance(content, list) and any(part.get("type") == "image_url" for part in content if isinstance(part, dict))


def llm_providers():
    providers = []
    if LLM_BASE_URL and LLM_MODEL:
        providers.append({"name": "primary", "base_url": LLM_BASE_URL, "model": LLM_MODEL, "api_key": LLM_API_KEY})
    if LLM_FALLBACK_BASE_URL and LLM_FALLBACK_MODEL:
        providers.append(
            {
                "name": "fallback",
                "base_url": LLM_FALLBACK_BASE_URL,
                "model": LLM_FALLBACK_MODEL,
                "api_key": LLM_FALLBACK_API_KEY,
            }
        )
    return providers


def llm_provider_label(provider):
    return f"{provider.get('name', 'llm')}:{provider.get('model', '')}"


def log_llm_output(content, has_images, provider):
    current_app.logger.info(
        "LLM raw output (%s, %s): %s",
        llm_provider_label(provider),
        "images" if has_images else "text",
        content,
    )


def log_llm_parsed(parsed, has_images, provider):
    current_app.logger.info(
        "LLM parsed JSON (%s, %s): %s",
        llm_provider_label(provider),
        "images" if has_images else "text",
        json.dumps(parsed, ensure_ascii=False, sort_keys=True),
    )


def log_llm_error(error, has_images, provider):
    current_app.logger.warning(
        "LLM parse failed (%s, %s): %s",
        llm_provider_label(provider),
        "images" if has_images else "text",
        error,
    )


def call_llm_chat(payload, provider):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = provider.get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = OPENROUTER_SITE_URL
    if OPENROUTER_APP_NAME:
        headers["X-Title"] = ascii_header_value(OPENROUTER_APP_NAME)
    request = Request(llm_chat_completions_url(provider["base_url"]), data=body, headers=headers, method="POST")
    with urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read().decode("utf-8"))
    message = data["choices"][0].get("message", {})
    for key in ("content", "response", "text"):
        value = message.get(key)
        if value:
            return value
    reasoning = message.get("reasoning_content", "")
    if "{" in reasoning and "}" in reasoning:
        return reasoning
    return ""


def llm_chat_completions_url(base_url):
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def ascii_header_value(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").strip()
    return ascii_value or "Home Renovation Receipts"


def content_disposition(disposition, filename):
    filename = Path(str(filename or "file")).name or "file"
    fallback = ascii_header_value(filename).replace('"', "")
    if not fallback:
        fallback = "file"
    encoded = quote(filename, safe="", encoding="utf-8")
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def prepare_ocr_for_llm(text):
    blocks = split_ocr_blocks(text)
    if not blocks:
        return text

    main_blocks = []
    prepayment_blocks = []
    for block in blocks:
        if is_prepayment_block(block):
            prepayment_blocks.append(block)
        else:
            main_blocks.append(block)

    if not main_blocks or not prepayment_blocks:
        return text

    summary = "\n".join(extract_prepayment_summary(block) for block in prepayment_blocks)
    summary = "\n".join(line for line in summary.splitlines() if line.strip())
    suffix = f"\n\n--- RELATED PREPAYMENT PAGES OMITTED AS MAIN RECEIPT SOURCE ---\n{summary}" if summary else ""
    return "\n\n".join(main_blocks) + suffix


def split_ocr_blocks(text):
    matches = list(re.finditer(r"(?m)^--- .+? ---$", text))
    if not matches:
        return [text] if text.strip() else []

    blocks = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append(text[start:end].strip())
    return blocks


def is_prepayment_block(text):
    lower = text.lower()
    return any(term in lower for term in ("račun za predplačilo", "racun za predplacilo", "vrednost predplačila", "vrednost predplacila"))


def extract_prepayment_summary(text):
    lines = []
    for line in text.splitlines():
        lower = line.lower()
        if any(
            term in lower
            for term in (
                "račun za predplačilo",
                "racun za predplacilo",
                "številka",
                "stevilka",
                "vrednost predpla",
                "mastercard",
                "naziv artikla",
            )
        ):
            cleaned = clean_line(line)
            if cleaned:
                lines.append(f"- {cleaned}")
    return "\n".join(lines[:12])


def extract_json_object(content):
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def clean_model_string(value):
    if value is None:
        return ""
    return str(value).strip()


def clean_model_vendor(value):
    vendor = clean_model_string(value)
    if vendor.lower() in {"unknown", "null", "none", "n/a", "neznano", "ni znano"}:
        return ""
    return strip_vendor_legal_suffix(vendor)


def strip_vendor_legal_suffix(vendor):
    vendor = clean_model_string(vendor)
    if not vendor:
        return ""
    suffixes = (
        r",?\s*d\.?\s*o\.?\s*o\.?",
        r",?\s*s\.?\s*p\.?",
        r",?\s*d\.?\s*d\.?",
        r",?\s*k\.?\s*d\.?",
        r",?\s*d\.?\s*n\.?\s*o\.?",
        r",?\s*ltd\.?",
        r",?\s*llc\.?",
        r",?\s*gmbh",
    )
    cleaned = vendor
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            next_value = re.sub(rf"\s*{suffix}\s*$", "", cleaned, flags=re.IGNORECASE).strip(" ,.-")
            if next_value != cleaned:
                cleaned = next_value
                changed = True
    return cleaned or vendor


def strip_vendor_from_description(description, vendor):
    description = clean_model_string(description)
    vendor = clean_model_string(vendor)
    if not description or not vendor:
        return description or "Receipt"

    vendor_tokens = {
        vendor.lower(),
        vendor.lower().replace(" trgovina, d.o.o.", ""),
        vendor.lower().replace(" d.o.o.", ""),
        vendor.lower().replace(" s.p.", ""),
    }
    cleaned = description
    for token in sorted(vendor_tokens, key=len, reverse=True):
        if not token:
            continue
        cleaned = re.sub(rf"^\s*{re.escape(token)}\s*[-–:|,]*\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" -–:|,") or default_description("")


def default_description(text):
    lower = text.lower()
    if any(word in lower for word in ("trgovina", "naziv artikla", "količina", "kolicina")):
        return "Material za prenovo"
    if any(word in lower for word in ("vodovod", "cev", "radiator")):
        return "Instalacijski material"
    return "Material za prenovo"


def extract_notes(text):
    lines = []
    in_items = False
    for line in text.splitlines():
        cleaned = clean_line(line)
        if not cleaned:
            continue
        lower = cleaned.lower()

        if "naziv artikla" in lower or ("količina" in lower and "em" in lower):
            in_items = True
            continue
        if any(marker in lower for marker in ("davčna osnova", "vrednost ddv", "skupaj", "prejeta plačila", "biz zate")):
            in_items = False

        if looks_like_item_line(cleaned, in_items):
            simplified = re.sub(r"^\d+\s+", "", cleaned)
            simplified = simplify_item_note(simplified)
            if simplified:
                lines.append(f"- {simplified}")

    unique = []
    for line in lines:
        if line not in unique:
            unique.append(line)
    return "\n".join(unique[:10])


def merge_item_notes(model_notes, extracted_notes):
    by_key = {}
    for source in (model_notes, extracted_notes):
        for line in clean_model_string(source).splitlines():
            cleaned = normalize_note_line(line)
            if not cleaned or not is_item_note(cleaned):
                continue
            if not cleaned.startswith("-"):
                cleaned = f"- {cleaned}"
            key = note_key(cleaned)
            current = by_key.get(key)
            if current is None or note_quality(cleaned) > note_quality(current):
                by_key[key] = cleaned
    return "\n".join(list(by_key.values())[:10])


def is_item_note(line):
    lower = line.lower()
    blocked = (
        "plačilo",
        "placilo",
        "mastercard",
        "visa",
        "maestro",
        "gotovina",
        "kartica",
        "račun",
        "racun",
        "ref",
        "predpla",
        "skupaj",
        "total",
        "datum",
        "ddv",
        "davčna",
        "davcna",
        "naslov",
        "kupec",
        "stranka",
        "garanc",
        "popust",
    )
    if any(token in lower for token in blocked):
        return False
    words = normalized_note_words(lower)
    return len(words) >= 1


def note_key(line):
    lower = line.lower()
    if "plačilo" in lower or "placilo" in lower or "mastercard" in lower or "gotovina" in lower:
        return "payment"
    if "račun" in lower or "racun" in lower:
        return "reference"
    if "predpla" in lower:
        return "prepayment"
    words = normalized_note_words(lower)
    return "item:" + " ".join(words[:4])


def normalized_note_words(value):
    normalized = re.sub(r"[^a-z0-9čšžćđ]+", " ", value.lower())
    stopwords = {
        "za",
        "in",
        "na",
        "z",
        "s",
        "po",
        "kos",
        "m2",
        "cnt",
        "kom",
        "em",
        "material",
        "kupljeno",
        "purchased",
    }
    words = []
    for word in normalized.split():
        if word in stopwords:
            continue
        if word.isdigit() or re.fullmatch(r"\d+[,.]?\d*", word):
            continue
        words.append(word)
    return words


def looks_like_item_line(line, in_items):
    lower = line.lower()
    if any(word in lower for word in ("akcija", "popust", "davčna", "vrednost", "skupaj", "mastercard", "račun", "ddv")):
        return False
    has_barcode = bool(re.search(r"\b\d{8,14}\b", line))
    has_quantity = bool(re.search(r"\b\d{1,3},\d{2}\s*(?:kos|m2|cnt|kom)\b", lower))
    has_letters = bool(re.search(r"[A-Za-zČŠŽčšž]", line))
    return has_letters and (has_barcode or has_quantity or (in_items and len(line) > 12))


def note_quality(line):
    text = line.strip("- ")
    score = 200 - len(text)
    lower = text.lower()
    if any(char.isdigit() for char in text):
        score -= 20
    if any(word in lower for word in ("plačilo", "placilo", "račun", "racun")):
        score += 50
    if len(text) < 18:
        score -= 30
    return score


def simplify_item_note(value):
    value = clean_line(value)
    value = re.sub(r"\b\d{8,14}\b", "", value)
    value = re.sub(r"\b\d{1,3},\d{2}\s*(?:KOS|M2|CNT)\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\b\d{1,4}[,.]\d{2,4}\b", "", value)
    value = re.sub(r"\b22,00\b|\b10,00%\b", "", value)
    value = re.sub(r"\s+", " ", value).strip(" -:;,")
    return value[:110]


def normalize_note_line(line):
    cleaned = clean_line(line).strip("- ")
    cleaned = re.sub(r"^\*\s*", "", cleaned)
    return f"- {cleaned}" if cleaned else ""


def is_important_note(line):
    lower = line.lower()
    reject = (
        "garancija",
        "zakon",
        "odpad",
        "embalaž",
        "embalaz",
        "slopak",
        "surovina",
        "biz status",
        "staro stanje",
        "novo stanje",
        "ident. št",
        "ddv:",
        "naslov",
        "kupec",
        "hvala",
        "pravico",
        "zamenjava",
    )
    if any(word in lower for word in reject):
        return False
    keep = (
        "plačilo",
        "placilo",
        "mastercard",
        "visa",
        "maestro",
        "gotovina",
        "račun",
        "racun",
        "predpla",
    )
    if any(word in lower for word in keep):
        return True
    words = normalized_note_words(lower)
    return 1 <= len(words) <= 10 and len(lower) <= 140


def correct_category(text, category):
    lower = text.lower()
    if category == "Gradbena dela" and any(word in lower for word in ("trgovina", "račun", "racun", "naziv artikla", "količina", "kolicina")):
        return "Materiali"
    return category


def parse_model_money(value):
    if value is None:
        return None
    return parse_money(str(value))


def prefer_main_receipt_reference(text, model_reference):
    references = extract_receipt_references(text)
    main_refs = references.get("main", [])
    if main_refs:
        return main_refs[0]
    if is_non_invoice_reference(model_reference) or not looks_like_invoice_reference(model_reference):
        return ""
    return model_reference


def is_non_invoice_reference(reference):
    lower = clean_model_string(reference).lower()
    return any(token in lower for token in ("posel", "veza", "kupec", "ddv", "biz", "kartica"))


def looks_like_invoice_reference(reference):
    value = clean_model_string(reference)
    return bool(re.search(r"\d{2,4}[-/]\d{1,3}[-/]\d{3,6}", value))


def extract_receipt_references(text):
    refs = {"main": [], "prepayment": []}
    prepayment_start = find_prepayment_start(text)
    for match in re.finditer(r"(?i)(račun\s+za\s+predplačilo|racun\s+za\s+predplacilo|račun|racun|številka|stevilka)\D{0,25}(\d{2,4}[-/]\d{1,3}[-/]\d{3,6})", text):
        label = match.group(1).lower()
        ref = match.group(2)
        key = "prepayment" if "predpla" in label or (prepayment_start is not None and match.start() > prepayment_start) else "main"
        if ref not in refs[key]:
            refs[key].append(ref)

    for match in re.finditer(r"\b(\d{3}-\d{2}-\d{5})\b", text):
        ref = match.group(1)
        key = "prepayment" if prepayment_start is not None and match.start() > prepayment_start else "main"
        if ref not in refs[key]:
            refs[key].append(ref)
    refs["main"] = [ref for ref in refs["main"] if ref not in refs["prepayment"]]
    return refs


def find_prepayment_start(text):
    match = re.search(r"(?i)račun\s+za\s+predplačilo|racun\s+za\s+predplacilo", text)
    return match.start() if match else None


def is_prepayment_only_total(text, total):
    if total is None:
        return False
    lower = text.lower()
    if not any(term in lower for term in ("račun za predplačilo", "racun za predplacilo", "predplačilo", "predplacilo")):
        return False
    if extract_received_payment_total(text) is not None:
        return False

    prepayment_amounts = []
    for line in text.splitlines():
        lower_line = line.lower()
        if "predpla" in lower_line or "vrednost predpla" in lower_line:
            prepayment_amounts.extend(extract_amounts(line))
        if "mastercard" in lower_line and "prejeta pla" not in lower:
            prepayment_amounts.extend(extract_amounts(line))
    return Decimal(str(total)) in prepayment_amounts


def first_vendor_line(lines):
    skip = ("racun", "račun", "datum", "ddv", "eur", "skupaj", "total")
    for line in lines[:10]:
        lower = line.lower()
        if len(line) >= 3 and not any(word in lower for word in skip) and not re.search(r"\d{2}[./-]\d{2}", line):
            return strip_vendor_legal_suffix(line[:120])
    return ""


def extract_total(text):
    payment_total = extract_received_payment_total(text)
    if payment_total is not None:
        return payment_total

    candidates = []
    total_words = r"(skupaj|za\s+plačilo|za placilo|total|amount|znesek)"
    for line in text.splitlines():
        normalized = line.lower()
        amounts = extract_amounts(line)
        if amounts and re.search(total_words, normalized):
            candidates.extend(amounts)
    if candidates:
        return max(candidates)

    amounts = extract_amounts(text)
    return max(amounts) if amounts else None


def extract_received_payment_total(text):
    lines = [clean_line(line) for line in text.splitlines()]
    payment_section = False
    payment_amounts = []
    payment_words = ("mastercard", "visa", "maestro", "gotovina", "kartica", "prejeta plačila", "prejeta placila")

    for line in lines:
        lower = line.lower()
        if "prejeta plačila" in lower or "prejeta placila" in lower:
            payment_section = True
            continue

        amounts = extract_amounts(line)
        if not amounts:
            continue

        if payment_section and ("skupaj plačila" in lower or "skupaj placila" in lower):
            return amounts[-1]

        if payment_section and any(word in lower for word in payment_words):
            payment_amounts.extend(amounts)

    if payment_amounts:
        return payment_amounts[-1]
    return None


def extract_amounts(text):
    amounts = []
    for match in re.finditer(r"(?<!\d)(\d{1,4}(?:[.,]\d{2}))(?!\d)", text):
        try:
            amounts.append(Decimal(match.group(1).replace(",", ".")))
        except InvalidOperation:
            pass
    return amounts


def extract_date(text):
    patterns = [
        r"\b(\d{1,2})[.](\d{1,2})[.](\d{4})\b",
        r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b",
        r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        groups = match.groups()
        if pattern.startswith("\\b(\\d{4})"):
            year, month, day = map(int, groups)
        else:
            day, month, year = map(int, groups)
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            continue
    return ""


def extract_reference(text):
    patterns = [
        r"(?:račun|racun|invoice|št\.?|st\.?|ref\.?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9./-]{3,})",
        r"\b([A-Z]{1,4}-?\d{4,}[-/]\d{2,})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)[:80]
    return ""


def suggest_category(text):
    lower = text.lower()
    category_words = {
        "Gradbena dela": ["gradnje", "rušenje", "rusenje", "betoniranje", "izvajalec"],
        "Materiali": ["merkur", "bauhaus", "obi", "top dom", "ytong", "vijak", "malta", "lepilo", "železo", "zelezo"],
        "Instalacije": ["vodovod", "elektro", "radiator", "cev", "instalacij"],
        "Pohištvo / oprema": ["pohištvo", "pohistvo", "oprema", "kuhinja", "omara"],
        "Projektiranje": ["projekt", "statika", "dokumentacija", "arhitekt"],
    }
    for category, words in category_words.items():
        if any(word in lower for word in words):
            return category
    return "Ostalo"


def clean_line(line):
    return re.sub(r"\s+", " ", line).strip(" -:\t")


def normalize_form(form):
    total = parse_money(form.get("total"))
    ana = parse_money(form.get("ana_paid"))
    jakob = parse_money(form.get("jakob_paid"))

    has_split = ana is not None or jakob is not None
    if has_split:
        total = (ana or Decimal("0")) + (jakob or Decimal("0"))

    return {
        "description": form.get("description", "").strip(),
        "category": normalize_category(form.get("category", "Ostalo")),
        "date": normalize_date(form.get("date", "")),
        "vendor": form.get("vendor", "").strip(),
        "reference": form.get("reference", "").strip(),
        "total": total,
        "ana_paid": ana or Decimal("0"),
        "jakob_paid": jakob or Decimal("0"),
        "notes": form.get("notes", "").strip(),
        "file_name": form.get("file_name", "").strip(),
        "file_names": form.get("file_names", "[]").strip(),
        "ocr_text": form.get("ocr_text", "").strip(),
    }


def parse_money(value):
    value = (value or "").strip().replace("€", "").replace(" ", "").replace(",", ".")
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def validate_receipt(data):
    errors = []
    if not data["description"]:
        errors.append("Description is required.")
    if not data["category"]:
        errors.append("Category is required.")
    if data["total"] is None:
        errors.append("Total is required.")
    if data["date"]:
        try:
            datetime.strptime(data["date"], "%Y-%m-%d")
        except ValueError:
            errors.append("Date must be dd.mm.yyyy.")
    return errors


def normalize_category(value):
    raw = (value or "").strip()
    aliases = {
        "material": "Materiali",
        "materiali": "Materiali",
        "ostalio": "Ostalo",
        "ostalo": "Ostalo",
        "pohistvo / oprema": "Pohištvo / oprema",
    }
    return aliases.get(raw.lower(), raw or "Ostalo")


def normalize_date(value):
    raw = (value or "").strip()
    if not raw:
        return ""
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return raw


def format_date(value):
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return raw


def iso_date(value):
    return normalize_date(value)


def insert_receipt(data):
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO receipts (
                description, category, date, vendor, reference, total, ana_paid,
                jakob_paid, notes, file_name, ocr_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["description"],
                data["category"],
                data["date"],
                data["vendor"],
                data["reference"],
                float(data["total"]),
                float(data["ana_paid"]),
                float(data["jakob_paid"]),
                data["notes"],
                data["file_name"],
                data["ocr_text"],
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        return cursor.lastrowid


def audit_log(action, receipt_id=None, details=None):
    created_at = datetime.utcnow().isoformat(timespec="seconds")
    payload = details or {}
    try:
        payload = dict(payload)
    except (TypeError, ValueError):
        payload = {"value": str(details)}
    if has_request_context():
        payload.setdefault("path", request.path)
        payload.setdefault("remote_addr", request.headers.get("X-Forwarded-For", request.remote_addr or ""))
    with get_db() as db:
        db.execute(
            "INSERT INTO audit_log (receipt_id, action, details, created_at) VALUES (?, ?, ?, ?)",
            (receipt_id, action, json.dumps(payload, ensure_ascii=False, sort_keys=True), created_at),
        )
    current_app.logger.info("Audit %s receipt=%s details=%s", action, receipt_id, payload)


def get_audit_logs(receipt_id, limit=25):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT action, details, created_at
            FROM audit_log
            WHERE receipt_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (receipt_id, limit),
        ).fetchall()
    logs = []
    for row in rows:
        item = dict(row)
        try:
            item["details_json"] = json.loads(item.get("details") or "{}")
        except json.JSONDecodeError:
            item["details_json"] = {}
        logs.append(item)
    return logs


def default_validation_status():
    return {
        "state": "unchecked",
        "label": "Not checked",
        "status": "",
        "issues": [],
        "created_at": "",
    }


def validation_status_from_log(log):
    if not log:
        return default_validation_status()
    try:
        details = json.loads(log["details"] or "{}")
    except json.JSONDecodeError:
        details = {}
    if log["action"] == "validate_failed":
        return {
            "state": "failed",
            "label": "Failed",
            "status": "failed",
            "issues": [],
            "created_at": log["created_at"],
        }
    status = clean_model_string(details.get("status")) or "checked"
    issues = details.get("issues") if isinstance(details.get("issues"), list) else []
    state = "issues" if issues else "ok"
    label = "Issues" if issues else status.capitalize()
    return {
        "state": state,
        "label": label,
        "status": status,
        "issues": issues,
        "created_at": log["created_at"],
    }


def get_latest_validation_status(receipt_id):
    with get_db() as db:
        row = db.execute(
            """
            SELECT action, details, created_at
            FROM audit_log
            WHERE receipt_id = ?
              AND action IN ('validate', 'validate_failed')
            ORDER BY id DESC
            LIMIT 1
            """,
            (receipt_id,),
        ).fetchone()
    return validation_status_from_log(row)


def get_latest_validation_statuses(receipt_ids):
    receipt_ids = [int(receipt_id) for receipt_id in receipt_ids]
    if not receipt_ids:
        return {}
    placeholders = ",".join("?" for _ in receipt_ids)
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT receipt_id, action, details, created_at
            FROM audit_log
            WHERE receipt_id IN ({placeholders})
              AND action IN ('validate', 'validate_failed')
            ORDER BY id DESC
            """,
            receipt_ids,
        ).fetchall()
    statuses = {}
    for row in rows:
        receipt_id = row["receipt_id"]
        if receipt_id in statuses:
            continue
        statuses[receipt_id] = validation_status_from_log(row)
    return statuses


def receipt_to_audit_dict(receipt):
    return {
        "description": receipt["description"],
        "category": receipt["category"],
        "date": receipt["date"],
        "vendor": receipt["vendor"],
        "reference": receipt["reference"],
        "total": money(receipt["total"]),
        "ana_paid": money(receipt["ana_paid"]),
        "jakob_paid": money(receipt["jakob_paid"]),
        "notes": receipt["notes"] or "",
    }


def receipt_data_to_audit_dict(data):
    return {
        "description": data["description"],
        "category": data["category"],
        "date": data["date"],
        "vendor": data["vendor"],
        "reference": data["reference"],
        "total": money(data["total"]),
        "ana_paid": money(data["ana_paid"]),
        "jakob_paid": money(data["jakob_paid"]),
        "notes": data["notes"],
    }


def insert_receipt_files(receipt_id, files):
    if not files:
        return
    with get_db() as db:
        for file in files:
            file_name = file.get("file_name", "").strip()
            upload_token = file.get("upload_token", "").strip()
            file_id = file.get("id")
            if not file_name and not upload_token and not file_id:
                continue
            updated = 0
            if upload_token:
                cursor = db.execute(
                    "UPDATE receipt_files SET receipt_id = ?, upload_token = NULL WHERE upload_token = ?",
                    (receipt_id, upload_token),
                )
                updated = cursor.rowcount
            elif file_id:
                cursor = db.execute(
                    "UPDATE receipt_files SET receipt_id = ?, upload_token = NULL WHERE id = ?",
                    (receipt_id, file_id),
                )
                updated = cursor.rowcount
            if updated:
                continue
            if not file_name:
                continue
            exists = db.execute(
                "SELECT 1 FROM receipt_files WHERE receipt_id = ? AND file_name = ?",
                (receipt_id, file_name),
            ).fetchone()
            if exists:
                continue
            db.execute(
                "INSERT INTO receipt_files (receipt_id, file_name, original_name, created_at) VALUES (?, ?, ?, ?)",
                (
                    receipt_id,
                    file_name,
                    file.get("original_name", file_name).strip() or file_name,
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            )


def update_receipt_by_id(receipt_id, data):
    with get_db() as db:
        db.execute(
            """
            UPDATE receipts
            SET description = ?,
                category = ?,
                date = ?,
                vendor = ?,
                reference = ?,
                total = ?,
                ana_paid = ?,
                jakob_paid = ?,
                notes = ?,
                file_name = ?,
                ocr_text = ?
            WHERE id = ?
            """,
            (
                data["description"],
                data["category"],
                data["date"],
                data["vendor"],
                data["reference"],
                float(data["total"]),
                float(data["ana_paid"]),
                float(data["jakob_paid"]),
                data["notes"],
                data["file_name"],
                data["ocr_text"],
                receipt_id,
            ),
        )


def get_receipt(receipt_id):
    with get_db() as db:
        return db.execute("SELECT * FROM receipts WHERE id = ? AND deleted_at IS NULL", (receipt_id,)).fetchone()


RECEIPT_SORTS = {
    "date": "COALESCE(date, created_at)",
    "amount": "total",
    "vendor": "LOWER(COALESCE(vendor, ''))",
    "category": "LOWER(COALESCE(category, ''))",
}


def receipt_filter_where(filters):
    clauses = []
    values = []
    q = filters.get("q") if filters else ""
    if q:
        clauses.append("(description LIKE ? OR vendor LIKE ? OR reference LIKE ? OR notes LIKE ?)")
        like = f"%{q}%"
        values.extend([like, like, like, like])
    category = filters.get("category") if filters else ""
    clauses.append("deleted_at IS NULL")
    if category:
        clauses.append("category = ?")
        values.append(category)
    vendor = filters.get("vendor") if filters else ""
    if vendor:
        clauses.append("vendor = ?")
        values.append(vendor)
    payer = filters.get("payer") if filters else ""
    if payer == "Ana":
        clauses.append("ana_paid > 0")
    elif payer == "Jakob":
        clauses.append("jakob_paid > 0")

    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where, values


def normalize_receipt_sort(sort, direction):
    sort = sort if sort in RECEIPT_SORTS else "date"
    direction = "asc" if direction == "asc" else "desc"
    return {"sort": sort, "direction": direction}


def receipt_order_clause(sort, direction):
    sort_state = normalize_receipt_sort(sort, direction)
    column = RECEIPT_SORTS[sort_state["sort"]]
    direction_sql = sort_state["direction"].upper()
    if sort_state["sort"] in {"vendor", "category"}:
        return f"{column} {direction_sql}, COALESCE(date, created_at) DESC, id DESC"
    return f"{column} {direction_sql}, id DESC"


def list_receipts(filters, sort="date", direction="desc", limit=None):
    where, values = receipt_filter_where(filters)
    order_clause = receipt_order_clause(sort, direction)
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT ?"
        values = values + [int(limit)]
    with get_db() as db:
        rows = db.execute(
            f"SELECT * FROM receipts {where} ORDER BY {order_clause}{limit_clause}",
            values,
        ).fetchall()
    validation_statuses = get_latest_validation_statuses([row["id"] for row in rows])
    receipts = []
    for row in rows:
        receipt = dict(row)
        receipt["files"] = get_receipt_files(receipt["id"])
        receipt["validation_status"] = validation_statuses.get(receipt["id"], default_validation_status())
        receipts.append(receipt)
    return receipts


def get_receipt_totals(filters):
    where, values = receipt_filter_where(filters)
    with get_db() as db:
        row = db.execute(
            f"""
            SELECT COUNT(*) count,
                   COALESCE(SUM(total), 0) total,
                   COALESCE(SUM(ana_paid), 0) ana,
                   COALESCE(SUM(jakob_paid), 0) jakob
            FROM receipts
            {where}
            """,
            values,
        ).fetchone()
    return {"count": row["count"], "total": row["total"], "ana": row["ana"], "jakob": row["jakob"]}


def expense_sort_url(column):
    if not has_request_context():
        return url_for("expenses", sort=column, direction="desc")
    current_sort = request.args.get("sort", "date")
    current_direction = request.args.get("direction", "desc")
    next_direction = "asc" if current_sort == column and current_direction == "desc" else "desc"
    args = request.args.to_dict()
    args["sort"] = column
    args["direction"] = next_direction
    return url_for("expenses", **args)


def get_receipt_files(receipt_id):
    with get_db() as db:
        rows = db.execute(
            "SELECT id, file_name, original_name, content_type FROM receipt_files WHERE receipt_id = ? AND deleted_at IS NULL ORDER BY id",
            (receipt_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_file_by_name(file_name):
    safe_name = Path(file_name).name
    if not safe_name or safe_name != file_name:
        return None
    with get_db() as db:
        row = db.execute(
            """
            SELECT id, file_name, original_name, content_type, content
            FROM receipt_files
            WHERE file_name = ? AND deleted_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (safe_name,),
        ).fetchone()
    return dict(row) if row else None


def get_pending_or_saved_file(file):
    upload_token = str(file.get("upload_token", "")).strip()
    file_id = file.get("id")
    file_name = Path(str(file.get("file_name", "")).strip()).name
    with get_db() as db:
        row = None
        if upload_token:
            row = db.execute(
                """
                SELECT id, file_name, original_name, content_type, content
                FROM receipt_files
                WHERE upload_token = ?
                """,
                (upload_token,),
            ).fetchone()
        if row is None and file_id:
            row = db.execute(
                """
                SELECT id, file_name, original_name, content_type, content
                FROM receipt_files
                WHERE id = ?
                """,
                (file_id,),
            ).fetchone()
        if row is None and file_name:
            row = db.execute(
                """
                SELECT id, file_name, original_name, content_type, content
                FROM receipt_files
                WHERE file_name = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (file_name,),
            ).fetchone()
    return dict(row) if row else None


def files_to_json(files):
    return json.dumps(files or [])


def parse_files_json(value):
    try:
        files = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(files, list):
        return []
    clean = []
    for file in files:
        if not isinstance(file, dict):
            continue
        file_name = str(file.get("file_name", "")).strip()
        if not file_name:
            continue
        original_name = str(file.get("original_name", file_name)).strip() or file_name
        clean_file = {"file_name": file_name, "original_name": original_name}
        if file.get("id"):
            clean_file["id"] = file.get("id")
        upload_token = str(file.get("upload_token", "")).strip()
        if upload_token:
            clean_file["upload_token"] = upload_token
        if file.get("content_type"):
            clean_file["content_type"] = str(file.get("content_type", "")).strip()
        clean.append(clean_file)
    return clean


def get_summary():
    with get_db() as db:
        totals = db.execute(
            "SELECT COALESCE(SUM(total), 0) total, COALESCE(SUM(ana_paid), 0) ana, COALESCE(SUM(jakob_paid), 0) jakob FROM receipts WHERE deleted_at IS NULL"
        ).fetchone()
        categories = db.execute(
            "SELECT category, SUM(total) total, SUM(ana_paid) ana, SUM(jakob_paid) jakob FROM receipts WHERE deleted_at IS NULL GROUP BY category ORDER BY total DESC"
        ).fetchall()
        vendors = db.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(vendor), ''), 'Neznano') vendor,
                   COUNT(*) count,
                   SUM(total) total,
                   SUM(ana_paid) ana,
                   SUM(jakob_paid) jakob
            FROM receipts
            WHERE deleted_at IS NULL
            GROUP BY COALESCE(NULLIF(TRIM(vendor), ''), 'Neznano')
            ORDER BY total DESC
            LIMIT 12
            """
        ).fetchall()
    return {"total": totals["total"], "ana": totals["ana"], "jakob": totals["jakob"], "categories": categories, "vendors": vendors}


def get_categories():
    with get_db() as db:
        rows = db.execute(
            "SELECT DISTINCT category FROM receipts WHERE deleted_at IS NULL AND TRIM(COALESCE(category, '')) != '' ORDER BY category"
        ).fetchall()
    existing = [row["category"] for row in rows]
    return sorted(set(CATEGORIES + existing), key=str.casefold)


def get_vendors():
    with get_db() as db:
        rows = db.execute(
            "SELECT DISTINCT vendor FROM receipts WHERE deleted_at IS NULL AND TRIM(COALESCE(vendor, '')) != '' ORDER BY vendor"
        ).fetchall()
    return [row["vendor"] for row in rows]


def delete_receipt_by_id(receipt_id):
    now = datetime.utcnow().isoformat(timespec="seconds")
    with get_db() as db:
        db.execute("UPDATE receipts SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (now, receipt_id))


def delete_receipt_file_by_name(receipt_id, file_name):
    now = datetime.utcnow().isoformat(timespec="seconds")
    with get_db() as db:
        db.execute(
            "UPDATE receipt_files SET deleted_at = ? WHERE receipt_id = ? AND file_name = ? AND deleted_at IS NULL",
            (now, receipt_id, file_name),
        )


def list_deleted_receipts():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM receipts WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC, id DESC"
        ).fetchall()
    receipts = []
    for row in rows:
        receipt = dict(row)
        receipt["files"] = get_receipt_files(receipt["id"])
        receipts.append(receipt)
    return receipts


def restore_receipt_by_id(receipt_id):
    with get_db() as db:
        cursor = db.execute("UPDATE receipts SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL", (receipt_id,))
    return cursor.rowcount > 0


def delete_uploaded_file(file_name):
    safe_name = Path(file_name).name
    if not safe_name or safe_name != file_name:
        return
    path = UPLOAD_DIR / safe_name
    if path.exists() and path.is_file():
        path.unlink()


def money(value):
    if value in (None, ""):
        return ""
    return f"{Decimal(str(value)):.2f}"


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    create_app().run(host="0.0.0.0", port=8000, debug=debug)
