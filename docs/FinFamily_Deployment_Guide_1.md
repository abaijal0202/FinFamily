# FinFamily — GCP VM Deployment Guide (Field-Tested Edition)

This is the complete, step-by-step process to deploy FinFamily on a GCP Compute
Engine VM, rewritten to bake in fixes for every issue hit during the actual
deployment: SSH username mismatches, `gcloud` auth-scope errors, and the
Nginx-to-Gunicorn socket permission failure behind the 502 error.

Estimated time: 45–60 minutes.

---

## 0. Before you start — the one root cause behind most of this

**Multiple SSH keys under different usernames can exist in the same GCP project.**
`gcloud compute scp` and `gcloud compute ssh`, if run without an explicit username,
can each independently resolve to a *different* Linux user on the VM — so a file
uploaded via `scp` can silently land in a different home directory than the one you
land in via `ssh`.

**The fix, applied throughout this guide: always specify the username explicitly,
every time, in every command.** In this guide that username is `abaij`. Confirm
yours with:

```bash
gcloud compute ssh finfamily-vm --zone=asia-south1-a --command="whoami"
```

Use whatever that prints in place of `abaij` everywhere below.

---

## 1. Create the VM

Run **locally** (your own machine, not inside any VM):

```bash
gcloud compute instances create finfamily-vm \
  --zone=asia-south1-a \
  --machine-type=e2-small \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB \
  --tags=http-server,https-server
```

`asia-south1-a` (Mumbai) keeps data in India, consistent with the BRD's
data-localization requirement.

Open the firewall for web traffic:

```bash
gcloud compute firewall-rules create allow-http --allow=tcp:80 --target-tags=http-server
gcloud compute firewall-rules create allow-https --allow=tcp:443 --target-tags=https-server
```

## 2. Connect to the VM — always with an explicit username

```bash
gcloud compute ssh abaij@finfamily-vm --zone=asia-south1-a
```

Confirm you landed where you expect:

```bash
whoami
pwd
```

> **Why the explicit username matters:** if you run `gcloud compute ssh finfamily-vm`
> without a username, gcloud may pick a *different* registered key/user than the one
> `scp` used in the next step, and you'll be looking at the wrong home directory
> later wondering where your file went.

## 3. Install system packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip nginx git unzip ufw
```

## 4. Upload the application code — with the same explicit username

From your **local machine**:

```bash
gcloud compute scp finfamily_webapp.zip abaij@finfamily-vm:~ --zone=asia-south1-a
```

If you're unsure whether it worked, don't just trust the progress bar — check the
actual exit code and destination:

```bash
echo $LASTEXITCODE          # PowerShell — 0 means success
```

or re-run with debug output to see exactly which user/path it used:
```bash
gcloud compute scp finfamily_webapp.zip abaij@finfamily-vm:~ --zone=asia-south1-a --verbosity=debug
```
Look for the line starting `Executing command: [...pscp.exe... abaij@<IP>:~]` and
confirm the username matches what you used to `ssh` in.

Back on the VM (connected as `abaij`):

```bash
ls -la ~
unzip finfamily_webapp.zip
cd ~/finfamily
```

If the zip isn't there, search the whole disk rather than guessing:
```bash
find / -name "finfamily_webapp.zip" 2>/dev/null
```

## 5. Create a virtual environment and install dependencies

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

Generate and set a strong `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```
Paste the result into `.env`. Leave `DATABASE_URL` on SQLite unless using Postgres.

## 7. Initialize the database and smoke-test locally

```bash
source venv/bin/activate
python3 -c "from app import app; from models import db; app.app_context().push(); db.create_all(); print('DB ready')"
python3 wsgi.py &
curl http://127.0.0.1:5000/login
kill %1
```
Expect HTML containing "Log in". A traceback here means fix it before continuing —
everything downstream (Nginx, systemd) will just mask the same error as a 502.

## 8. Run the app with Gunicorn under systemd

```bash
sudo nano /etc/systemd/system/finfamily.service
```

Paste this **exactly** — note the `ExecStartPre` and `Umask` lines. These aren't
optional extras; skipping them is what caused the 502/Permission denied error in
this deployment:

```ini
[Unit]
Description=FinFamily Flask application
After=network.target

[Service]
User=abaij
Group=www-data
WorkingDirectory=/home/abaij/finfamily
EnvironmentFile=/home/abaij/finfamily/.env
ExecStartPre=/bin/rm -f /home/abaij/finfamily/finfamily.sock
Umask=007
ExecStart=/home/abaij/finfamily/venv/bin/gunicorn \
    --workers 3 \
    --bind unix:/home/abaij/finfamily/finfamily.sock \
    wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

Why each of these matters:
- **`Group=www-data`** — lets the Nginx process (which runs as `www-data`) read/write the socket.
- **`ExecStartPre=/bin/rm -f ...`** — deletes any stale socket file left with old permissions before each start; systemd won't always recreate it cleanly otherwise.
- **`Umask=007`** — ensures the *newly created* socket file is group-readable/writable (owner + group, no "other"), instead of defaulting to owner-only.

Start it:

```bash
sudo systemctl daemon-reload
sudo systemctl start finfamily
sudo systemctl enable finfamily
sudo systemctl status finfamily
```

Expect `active (running)`. If not:
```bash
sudo journalctl -u finfamily -n 50 --no-pager
```

## 9. Fix home-directory traversal permissions (Ubuntu default blocks this)

Ubuntu creates home directories as `750` by default — owner-only. Even with a
correctly-permissioned socket, Nginx (`www-data`) can't traverse into
`/home/abaij/` to reach it unless the "execute" bit is opened for others:

```bash
chmod o+x /home/abaij
chmod o+x /home/abaij/finfamily
```

This only grants directory *traversal*, not file listing/reading — safe to apply.

Verify the whole path is now walkable:
```bash
namei -om /home/abaij/finfamily/finfamily.sock
```
Every directory in the output should show an `x` bit available to `www-data`
(either via "other" or its group).

Verify the socket itself:
```bash
ls -la /home/abaij/finfamily/finfamily.sock
```
Expect something like:
```
srw-rw---- 1 abaij www-data 0 ... finfamily.sock
```

## 10. Configure Nginx as a reverse proxy

```bash
sudo nano /etc/nginx/sites-available/finfamily
```

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 5M;

    location /static/ {
        alias /home/abaij/finfamily/static/;
    }

    location / {
        include proxy_params;
        proxy_pass http://unix:/home/abaij/finfamily/finfamily.sock;
    }
}
```

Enable it:

```bash
sudo ln -s /etc/nginx/sites-available/finfamily /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```

## 11. Open the VM's own firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

## 12. Find the external IP — run this locally, not on the VM

```bash
gcloud compute instances describe finfamily-vm --zone=asia-south1-a \
  --format="get(networkInterfaces[0].accessConfigs[0].natIP)"
```

> **Do not run `gcloud compute` commands from inside the VM.** The VM's default
> service account typically lacks the `compute.readonly` scope, so this fails with
> `Request had insufficient authentication scopes` when run over SSH on the
> instance itself. If you ever need `gcloud` to work from inside the VM, either run
> `gcloud auth login` there to use your own user credentials, or stop the VM and
> update its service-account scopes with
> `gcloud compute instances set-service-account`.

Visit `http://<external-ip>/` — you should see the FinFamily login page.

## 13. If you still get a 502 — full diagnostic checklist

Work through these in order; each one caught a real failure in this deployment:

```bash
# 1. Is the app process actually running?
sudo systemctl status finfamily
sudo journalctl -u finfamily -n 50 --no-pager

# 2. Does the socket file exist?
ls -la /home/abaij/finfamily/finfamily.sock

# 3. What does Nginx say the specific failure is?
sudo tail -30 /var/log/nginx/error.log
# "Permission denied" -> socket/traversal permissions (steps 9 above)
# "No such file or directory" -> socket path mismatch or app never started
# "Connection refused" -> app bound to wrong address/port, or crashed

# 4. Do both config files reference the identical socket path?
grep sock /etc/systemd/system/finfamily.service
grep sock /etc/nginx/sites-available/finfamily

# 5. After any fix, always force a clean restart (don't just reload):
sudo systemctl daemon-reload
sudo systemctl stop finfamily
rm -f /home/abaij/finfamily/finfamily.sock
sudo systemctl start finfamily
sudo systemctl reload nginx
curl -I http://127.0.0.1/
```

## 14. (Recommended) Add HTTPS once a domain points at the VM

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

Given this app stores financial data, HTTPS is not optional beyond a local demo
(BRD NFR-SEC-01).

## 15. (Optional) Switch from SQLite to PostgreSQL / Cloud SQL

```bash
gcloud sql instances create finfamily-db \
  --database-version=POSTGRES_15 --tier=db-f1-micro --region=asia-south1
gcloud sql databases create finfamily --instance=finfamily-db
gcloud sql users set-password postgres --instance=finfamily-db --password=<STRONG_PASSWORD>
```

Install the Cloud SQL Auth Proxy on the VM, update `.env`:
```
DATABASE_URL=postgresql://postgres:<STRONG_PASSWORD>@127.0.0.1:5432/finfamily
```
Uncomment `psycopg2-binary` in `requirements.txt`, reinstall, then:
```bash
python3 -c "from app import app; from models import db; app.app_context().push(); db.create_all()"
sudo systemctl restart finfamily
```

---

## Quick-reference: issues encountered and their fixes

| Symptom | Root Cause | Fix |
|---|---|---|
| Uploaded file "successfully" but can't be found | `scp` and `ssh` resolved to different usernames from multiple SSH keys in the project | Always specify `user@instance` explicitly in both commands |
| `Request had insufficient authentication scopes` | Ran a `gcloud compute` query command from inside the VM, using its limited-scope service account | Run `gcloud compute` describe/query commands locally, not over SSH on the VM |
| `502 Bad Gateway` | Gunicorn socket unreachable by Nginx (`www-data`) | Set `Group=www-data`, `Umask=007`, `ExecStartPre=rm -f <socket>` in the systemd unit; `chmod o+x` on the home and project directories for traversal |

## Ongoing operations cheat-sheet

| Task | Command |
|---|---|
| View live app logs | `sudo journalctl -u finfamily -f` |
| Restart app after code changes | `sudo systemctl restart finfamily` |
| Restart Nginx after config changes | `sudo systemctl restart nginx` |
| Back up the SQLite database | `cp ~/finfamily/finfamily.db ~/finfamily-backup-$(date +%F).db` |
| Deploy new code | Re-upload/`git pull`, `pip install -r requirements.txt` inside venv, `sudo systemctl restart finfamily` |
| Confirm socket permissions after any change | `ls -la ~/finfamily/finfamily.sock` and `namei -om ~/finfamily/finfamily.sock` |

## Notes on production-readiness vs. the BRD

This deployment gets a working, internet-reachable instance of the Phase-1 manual
tracking application. Before treating it as a real product handling other people's
financial data, still needed per the BRD's Non-Functional Requirements (Sec 9):
AES-256 encryption at rest for sensitive columns (NFR-SEC-01), MFA on login
(NFR-SEC-04), an immutable audit trail (NFR-SEC-07), registration as (or partnership
with) a licensed Account Aggregator FIU before any live bank/CAS/CRA integration,
and a DPDP Act, 2023 compliance review.
