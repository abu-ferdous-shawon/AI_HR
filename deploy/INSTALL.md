# HR AI Assistant: VM installation

Target: the Linux VM that runs Frappe HRMS v16 behind nginx.
Needs: sudo, Python 3.11+, outbound HTTPS to the LLM provider only.

## 0. Information to collect first
| Item | Example | Who gives it |
|---|---|---|
| Site name (bench) | hrm.example.com | Frappe admin |
| Bench path | /home/frappe/frappe-bench | Frappe admin |
| Site nginx file | /etc/nginx/conf.d/frappe-bench.conf | `bench setup nginx` output |
| Frappe API key + secret of the "AI Integration" user | (secret) | Frappe admin |
| LLM key | (secret) | Project owner |

## 1. Frappe side (once)
1. Create a User "ai-integration@yourcompany.com". Do NOT give System Manager.
   Give read-only access to the HR doctypes in the registry profile (Employee, Leave Application, Attendance, Salary Slip and the rest).
2. Open that user, Settings > API Access > Generate Keys. Save key and secret.
3. Every staff member who will use the chat needs a Frappe User with the Employee role,
   linked from Employee > User ID. Staff without a login cannot use the assistant (by design).
4. Managers: "Reports To" must be set on their team's Employee records.
5. HR staff: role "HR Manager" or "HR User".

## 2. Install the backend (run from the project folder copied to the VM)
```
sudo useradd --system --home /opt/hr-ai --shell /usr/sbin/nologin hrai
sudo mkdir -p /opt/hr-ai
sudo rsync -a --exclude .venv --exclude .env --exclude __pycache__ ./ /opt/hr-ai/
sudo chown -R root:hrai /opt/hr-ai && sudo chmod -R g+rX,o-rwx /opt/hr-ai
sudo python3 -m venv /opt/hr-ai/.venv
sudo /opt/hr-ai/.venv/bin/pip install -r /opt/hr-ai/requirements.txt
sudo install -d -m 750 -o root -g hrai /etc/hr-ai
sudo install -m 600 -o root -g root deploy/hr-ai.env.example /etc/hr-ai/hr-ai.env
sudo nano /etc/hr-ai/hr-ai.env
```
Fill in: FRAPPE_URL (the public https URL of the site), FRAPPE_API_KEY, FRAPPE_API_SECRET,
GEMINI_API_KEY. Keep ENVIRONMENT=production and DEV_MODE=false.
If the VM cannot resolve the site's public name, add it to /etc/hosts pointing at 127.0.0.1.

Copy the policy documents (PDF/DOCX/MD) into /opt/hr-ai/policies/.

```
sudo cp deploy/hr-ai.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hr-ai
sudo systemctl status hr-ai --no-pager
curl -s http://127.0.0.1:8100/health        # {"status":"ok"}
```

## 3. nginx
1. Open the site's nginx file.
2. Paste the `location /ai/ { ... }` block from deploy/nginx_frappe_ai.conf inside the
   `server { ... }` block, above `location /`.
3. Test and reload:
```
sudo nginx -t && sudo systemctl reload nginx
```
4. Check: `curl -s https://YOUR_SITE/ai/health` returns {"status":"ok"}.

## 4. Frappe widget app
```
cd /home/frappe/frappe-bench
bench get-app /opt/hr-ai/frappe_app/hr_ai_widget
bench --site YOUR_SITE install-app hr_ai_widget
bench build --app hr_ai_widget
bench --site YOUR_SITE clear-cache
bench restart
```
If apps cannot be installed: skip this, and enable the three commented `sub_filter`
lines from deploy/nginx_frappe_ai.conf instead, then reload nginx.

## 5. Acceptance checks
| # | Check | Expected |
|---|---|---|
| 1 | `sudo ss -ltnp \| grep 8100` | only 127.0.0.1:8100 |
| 2 | `curl -s -o /dev/null -w "%{http_code}" https://YOUR_SITE/ai/docs` | 404 |
| 3 | POST /ai/chat with header `Origin: https://evil.example` | 403 |
| 4 | `sudo stat -c '%a %U' /etc/hr-ai/hr-ai.env` | 600 root |
| 5 | `sudo tail -n 3 /var/log/hr-ai/app.log` | JSON lines |
| 6 | Set DEV_MODE=true, restart | service refuses to start |
| 7 | Log in as an employee, open /app | 💬 button visible; not on /login |
| 8 | Employee asks "How many casual leaves do I have left?" | correct number |
| 9 | Employee asks for company payroll cost | "HR only" |
| 10 | HR user asks the same | figure returned |
| 11 | Manager asks for team attendance | works; employee gets refused |
| 12 | `sudo systemctl stop hr-ai`, send a message | "Cannot reach the assistant" |

## 6. Updating later
```
sudo rsync -a --exclude .venv --exclude .env --exclude __pycache__ ./ /opt/hr-ai/
sudo systemctl restart hr-ai
```
The widget script updates itself (served by the backend). Only changes under
frappe_app/ need `bench build` again.

## 7. Rollback
`bench --site YOUR_SITE uninstall-app hr_ai_widget` removes the button.
`sudo systemctl disable --now hr-ai` stops the backend. Remove the `location /ai/` block and reload nginx.