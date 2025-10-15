# Queue Service

Simple Python queue management service designed to feed Xibo datasets. It exposes a FastAPI backend, persists to SQLite, and ships with a minimal admin UI for daily ticket issuance.

## Features

- REST API under the `/queue` prefix for easy HAProxy routing
- SQLite-backed storage keeping full historical queue data
- Daily ticket sequence resets with zero-padded numbers (`001`-`999`)
- Lightweight admin console (`/queue/admin`) with embedded JS/CSS for issuing tickets and starting a new day
- Xibo-friendly dataset endpoint (`/queue/display`) returning structured JSON for layouts or widgets

## Project layout

```
.
├── app/
│   ├── main.py        # FastAPI app and route handlers
│   ├── models.py      # SQLModel ORM definitions
│   ├── database.py    # Engine/session helpers
│   └── templates/     # HTML admin UI
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## Running locally

Create a virtual environment, install dependencies, and start the server:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 1111
```

The admin UI will be available at [http://localhost:1111/queue/admin](http://localhost:1111/queue/admin).

## Docker deployment

Build and run with Docker Compose (persists the SQLite database under `./data`):

```bash
docker compose up --build -d
```

The container exposes port `1111` for the HAProxy backend pool.

## API overview (prefix `/queue`)

- `POST /queue/start-day` — starts or resets the queue for a date. Payload `{ "service_date": "2024-05-20", "overwrite": true }`.
- `POST /queue/entries` — issues a ticket. Payload `{ "name": "Jane", "phone": "+15550100" }`.
- `POST /queue/xibo` — form-data variant of ticket creation for systems that submit URL-encoded payloads.
- `GET /queue/entries?service_date=2024-05-20` — lists tickets for a given date ordered by queue number.
- `GET /queue/display?service_date=2024-05-20` — JSON snapshot tailored for Xibo datasets:

```json
{
  "service_date": "2024-05-20",
  "count": 2,
  "queue": [
    { "ticket": "001", "name": "Jane", "phone": "+15550100" },
    { "ticket": "002", "name": "Alex", "phone": "+15559876" }
  ]
}
```

- `GET /queue/health` — simple health probe.

All dates default to today when omitted.

## Xibo integration notes

1. Configure a Remote DataSet in Xibo to poll `http://<host>:1111/queue/display`. The JSON structure above exposes the queue name and number for layout binding via `data.queue[n].ticket` and `data.queue[n].name`.
2. To issue tickets from Xibo using the DataSet "Data to add" feature, point it at `http://<host>:1111/queue/xibo` and include URL-encoded fields `name` and `phone`.
3. Use the admin console or API to reset the queue daily (the "New Day" button on the UI calls `/queue/start-day` with `overwrite=true`).

## Development tips

- Database files live under `./data`. Mount or back up this directory in production.
- Adjust HAProxy routing to forward `/queue` paths to the container on port 1111.
- Customize the admin UI by editing `app/templates/admin.html` (CSS/JS embedded for portability).

