# Home Renovation Receipt Tracker

A small Dockerized receipt archive and renovation cost tracker modeled after `PRENOVA_VESEL_SALMIC.xlsx`.

## Features

- Upload multiple receipt files with any extension.
- Mobile-friendly capture flow: add/photograph all receipt pages first, then run one combined OCR pass.
- Image/PDF receipt parsing through a multimodal LLM.
- Optional local LLM/llama-swap parsing to turn OCR text into structured fields.
- Review and correct extracted vendor, date, reference, total, category, payer split, and notes.
- Auto-calculates the total from the Ana/Jakob payer split.
- Learns vendors and categories from saved receipts for editable dropdown selection.
- Uses `dd.mm.yyyy` dates in the UI and CSV export.
- Dashboard totals by category and payer.
- Receipt image/file archive.
- Imports the starter rows from `app/seed/renovation_costs.csv`.
- CSV export for Excel.

## Run

```bash
docker compose up --build
```

Open:

```text
http://localhost:8000
```

Data is stored in `./data/receipts.sqlite3`, and uploaded receipt files are stored in `./data/uploads`. Keep that `data` folder backed up; it is the permanent archive. Deleting an attachment removes it from the database and deletes the stored file from disk.

The default upload limit is 128 MB per request. Change `MAX_UPLOAD_MB` in `docker-compose.yml` if you need larger batches.

## Deploy

On the machine that will host the app:

```bash
cd /home/jakob/HomeRenovation
docker compose up -d --build
docker compose logs -f receipts
```

Open the app from the host:

```text
http://localhost:8000
```

Open it from another device on your LAN:

```text
http://SERVER_IP:8000
```

For your current llama-swap setup, `docker-compose.yml` points the app at:

```text
http://192.168.10.10:9292
```

Useful maintenance commands:

```bash
docker compose ps
docker compose logs -f receipts
docker compose restart receipts
docker compose pull
docker compose up -d --build
```

Back up this folder regularly:

```text
/home/jakob/HomeRenovation/data
```

It contains both `receipts.sqlite3` and the permanent uploaded receipt files.

## TrueNAS Deploy Without Building

TrueNAS should use `docker-compose.truenas.yml`, which uses `image:` instead of `build:`. It is set up for:

```text
https://github.com/UnknownHero99/RecieptTracker
```

The GHCR image name is lowercase:

```yaml
image: ghcr.io/unknownhero99/reciepttracker:latest
```

Before deploying, replace these placeholders:

```yaml
volumes:
  - /mnt/YOUR_POOL/apps/home-renovation/data:/app/data
FLASK_SECRET_KEY: change-this-to-a-long-random-value
```

Build the image on another machine and push it to a registry:

```bash
cd /home/jakob/HomeRenovation
docker build -t ghcr.io/unknownhero99/reciepttracker:latest .
docker push ghcr.io/unknownhero99/reciepttracker:latest
```

Then on TrueNAS deploy with the compose file contents from `docker-compose.truenas.yml`. Updates are:

```bash
docker compose -f docker-compose.truenas.yml pull
docker compose -f docker-compose.truenas.yml up -d
```

Make sure the host path exists on TrueNAS before starting the app:

```text
/mnt/YOUR_POOL/apps/home-renovation/data
```

## Optional LLM parsing

If llama-swap exposes an OpenAI-compatible API, set these in `docker-compose.yml`:

```yaml
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=qwen/qwen3.6-flash
LLM_API_KEY=sk-or-replace-me
LLM_FALLBACK_BASE_URL=http://192.168.10.10:9292
LLM_FALLBACK_MODEL=Gemma4-E4B
LLM_FALLBACK_API_KEY=local
OPENROUTER_SITE_URL=
OPENROUTER_APP_NAME=Vesel Salmič
LLM_MAX_TOKENS=8192
LLM_OCR_CHARS=14000
LLM_SEND_IMAGES=1
LLM_IMAGE_MAX_FILES=6
LLM_IMAGE_MAX_PDF_PAGES=3
LLM_IMAGE_MAX_SIDE=1600
LLM_IMAGE_JPEG_QUALITY=85
BACKUP_INTERVAL_HOURS=24
BACKUP_RETENTION_DAYS=30
```

When configured, the app sends receipt images directly to the primary model through OpenRouter and uses the JSON result to prefill vendor, date, reference, category, description, and total. If the primary request fails, `LLM_FALLBACK_*` can point at a local OpenAI-compatible server such as llama-swap. The image limits keep requests from getting too large.

Backups are written automatically to `data/backups` as SQLite snapshots. `BACKUP_INTERVAL_HOURS=24` creates at most one automatic backup per day, and `BACKUP_RETENTION_DAYS=30` removes older backup files.

The parser asks the model to keep `Opis stroška` as a short summary and put purchased item details into `Opombe` as a list. If OCR contains `Prejeta plačila` or `Skupaj plačila`, that payment amount is used as the total, which handles Merkur receipts where tax-base/subtotal lines can be misleading.

## Notes

OCR and LLM parsing are intentionally treated as a draft. Receipts vary a lot, so the app pre-fills a form and lets you confirm the values before saving.
