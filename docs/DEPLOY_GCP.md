# Deploying FinFamily to a GCP Compute Engine VM (Ubuntu)

This deploys the app with **Gunicorn** (WSGI server) behind **Nginx** (reverse proxy +
TLS termination), managed by **systemd** so it survives reboots. SQLite is used by
default; a note at the end shows how to switch to Cloud SQL / PostgreSQL.

Estimated time: 30–45 minutes.

---

## 1. Create the VM

In the GCP Console, or via `gcloud`:

```bash
gcloud compute instances create finfamily-vm \
  --zone=asia-south1-a \
  --machine-type=e2-small \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB \
  --tags=http-server,https-server
```

- `asia-south1-a` (Mumbai) keeps latency low and data in India, consistent with the
  BRD's data-localization requirement (NFR — Compliance).
- `e2-small` (2 vCPU burst, 2 GB RAM) is enough for this app at small scale; resize
  later with `gcloud compute instances set-machine-type` if needed.

Open firewall ports for HTTP/HTTPS if not already open:

```bash
gcloud compute firewall-rules create allow-http --allow=tcp:80 --target-tags=http-server
gcloud compute firewall-rules create allow-https --allow=tcp:443 --target-tags=https-server
```

## 2. Connect to the VM

```bash
gcloud compute ssh finfamily-vm --zone=asia-south1-a
```

(Or use the "SSH" button in the Console, or a plain `ssh` command if you've added
your own key.)

## 3. Install system packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip nginx git ufw
sudo apt install -y tesseract-ocr poppler-utils   # required for statement-import OCR (Sec 8 fallback path)
```

## 4. Upload the application code

From your **local machine** (not the VM), copy the project to the VM. Pick one:

**Option A — `scp` the zip you downloaded from this chat:**
```bash
gcloud compute scp finfamily_webapp.zip finfamily-vm:~ --zone=asia-south1-a
```
Then on the VM:
```bash
sudo apt install -y unzip
unzip finfamily_webapp.zip
```

**Option B — if the code is in a Git repo:**
```bash
git clone <your-repo-url> finfamily
```

You should now have `~/finfamily/` on the VM containing `app.py`, `models.py`, etc.

## 5. Create a Python virtual environment and install dependencies

```bash
cd ~/finfamily
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 6. Configure environment variables

```bash
cp .env.example .env
nano .env
```

At minimum, set a strong `SECRET_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```
Paste the output as `SECRET_KEY=...` in `.env`. Leave `DATABASE_URL` on the default
SQLite line unless you're using PostgreSQL (see Section 11).

## 7. Initialize the database and do a smoke test

```bash
source venv/bin/activate
python3 -c "from app import app; from models import db; app.app_context().push(); db.create_all(); print('DB ready')"
python3 wsgi.py &
curl http://127.0.0.1:5000/login
kill %1
```
You should see HTML output containing "Log in". If you see a traceback instead,
re-check `.env` and the pip install step before continuing.

## 8. Run the app with Gunicorn under systemd

Create a systemd unit so the app starts automatically and restarts on crash/reboot:

```bash
sudo nano /etc/systemd/system/finfamily.service
```

Paste (adjust the `User` and paths if your username or clone path differ):

```ini
[Unit]
Description=FinFamily Flask application
After=network.target

[Service]
User=YOUR_LINUX_USERNAME
Group=www-data
WorkingDirectory=/home/YOUR_LINUX_USERNAME/finfamily
EnvironmentFile=/home/YOUR_LINUX_USERNAME/finfamily/.env
ExecStart=/home/YOUR_LINUX_USERNAME/finfamily/venv/bin/gunicorn \
    --workers 3 \
    --bind unix:/home/YOUR_LINUX_USERNAME/finfamily/finfamily.sock \
    wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

Find your username with `whoami` if unsure. Save and exit (Ctrl+O, Enter, Ctrl+X in nano).

Start and enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl start finfamily
sudo systemctl enable finfamily
sudo systemctl status finfamily
```

You should see `active (running)`. If not, check logs:
```bash
sudo journalctl -u finfamily -n 50 --no-pager
```

## 9. Configure Nginx as a reverse proxy

```bash
sudo nano /etc/nginx/sites-available/finfamily
```

Paste (replace `your-domain.com` with your domain, or your VM's external IP if you
don't have one yet):

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 20M;  # statement PDFs can be a few MB; matches MAX_CONTENT_LENGTH in config.py

    location /static/ {
        alias /home/YOUR_LINUX_USERNAME/finfamily/static/;
    }

    location / {
        include proxy_params;
        proxy_pass http://unix:/home/YOUR_LINUX_USERNAME/finfamily/finfamily.sock;
    }
}
```

Enable the site and test the config:

```bash
sudo ln -s /etc/nginx/sites-available/finfamily /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```

## 10. Open the firewall on the VM itself and verify

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

Find your VM's external IP:
```bash
gcloud compute instances describe finfamily-vm --zone=asia-south1-a \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

Visit `http://<external-ip>/` in a browser — you should see the FinFamily login
page. If it doesn't load, check `sudo systemctl status nginx` and
`sudo journalctl -u finfamily -n 50`.

## 11. (Recommended) Add HTTPS with a free Let's Encrypt certificate

Only works once a real domain name points at the VM's external IP (add an A record
in your DNS provider first).

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

Certbot edits the Nginx config automatically and sets up auto-renewal. Given this
app stores PAN-adjacent financial data, HTTPS is not optional for anything beyond a
local demo — see BRD NFR-SEC-01.

## 12. (Optional) Switch from SQLite to PostgreSQL

SQLite is fine for a single small VM and light usage, but a managed Postgres
instance is more resilient for real usage:

```bash
gcloud sql instances create finfamily-db \
  --database-version=POSTGRES_15 \
  --tier=db-f1-micro \
  --region=asia-south1

gcloud sql databases create finfamily --instance=finfamily-db
gcloud sql users set-password postgres --instance=finfamily-db --password=<STRONG_PASSWORD>
```

Install the Cloud SQL Auth Proxy on the VM, then update `.env`:
```
DATABASE_URL=postgresql://postgres:<STRONG_PASSWORD>@127.0.0.1:5432/finfamily
```
Uncomment `psycopg2-binary` in `requirements.txt`, `pip install -r requirements.txt`
again inside the venv, then:
```bash
python3 -c "from app import app; from models import db; app.app_context().push(); db.create_all()"
sudo systemctl restart finfamily
```

## 13. Ongoing operations cheat-sheet

| Task | Command |
|---|---|
| View live app logs | `sudo journalctl -u finfamily -f` |
| Restart app after code changes | `sudo systemctl restart finfamily` |
| Restart Nginx after config changes | `sudo systemctl restart nginx` |
| Back up the SQLite database | `cp ~/finfamily/finfamily.db ~/finfamily-backup-$(date +%F).db` |
| Deploy new code | `git pull` (or re-upload), `source venv/bin/activate && pip install -r requirements.txt`, then `sudo systemctl restart finfamily` |

## Notes on production-readiness vs. the BRD

This deployment gets a working, internet-reachable instance of the Phase-1 manual
tracking application. Before treating it as a real product handling other people's
financial data, still needed per the BRD's own Non-Functional Requirements (Sec 9):
AES-256 encryption at rest for sensitive columns (NFR-SEC-01), MFA on login
(NFR-SEC-04), an immutable audit trail (NFR-SEC-07), registration as (or partnership
with) a licensed Account Aggregator FIU before any live bank/CAS/CRA integration,
and a DPDP Act, 2023 compliance review. Treat this deployment as an internal/demo
environment until those are addressed.
