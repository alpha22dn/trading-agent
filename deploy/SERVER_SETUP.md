# Moving the Trading Agent to an Oracle Cloud server

About 30–45 minutes at a computer. You end up with the agent running around the clock on Oracle's free Arm server in
India, reachable only from your own devices (through Tailscale) at an `https://…ts.net` address, behind a password.
Nothing is opened to the internet.

The Kite login stays manual, once a day: Zerodha says exchange rules require you to log in yourself at least once a
day. On a server you do it from your phone: the 08:30 Telegram alert carries a link to the dashboard's Login screen.

---

## 1. Accounts (15 min)

**Oracle Cloud** (oracle.com/cloud/free)
1. Sign up. **Home region: India West (Mumbai) or India South (Hyderabad).** It cannot be changed later.
   A card is needed for verification.
2. Upgrade the account to **Pay As You Go** (Billing → Upgrade).
   - Always Free resources stay free.
   - Free-tier accounts have servers that look idle reclaimed. Oracle reclaims an Always Free server when its CPU,
     network and memory all stay under 20% for 7 days, and this agent is light enough to look idle.
   - Add a **budget alert** of ₹100 (Billing → Budgets) so any charge is flagged at once.

**Tailscale** (tailscale.com): create an account. Install the Tailscale app on your **phone and laptop** and sign in to
the same account. In the admin console → DNS: turn on **MagicDNS** and **HTTPS certificates**.

**Healthchecks.io** (optional, free; recommended):
1. Create a check with **period 5 minutes, grace 10 minutes**.
2. Under Integrations, add **Telegram**.
3. Copy the check's ping URL. It goes in `.env` as `HEALTHCHECK_URL` (step 4).

## 2. Create the server (10 min)

In the Oracle console: Compute → Instances → **Create instance**.

| Setting | Value |
|---|---|
| Image | **Canonical Ubuntu 24.04** |
| Shape | Ampere → **VM.Standard.A1.Flex**, **2 OCPU, 12 GB** memory (the free allowance) |
| Networking | default VCN, **assign a public IPv4 address** |
| SSH keys | **Generate a key pair** and **download the private key** (keep it safe) |
| Boot volume | **150 GB** (up to 200 GB total is free) |

If it says *Out of capacity*, pick another availability domain or try again later. Free Arm capacity in India
comes and goes.

Note the server's **public IP**. Live trading later needs this IP whitelisted in Kite (static IP rule); paper mode
does not.

## 3. Copy the agent and your data (10 min, on the laptop)

1. **Stop the agent on the laptop** (close the run.bat window) so the database files are consistent.
2. In PowerShell (Windows has `tar` and `ssh` built in), pack the folder without the Windows-only Python
   environment:

   ```powershell
   cd "D:\AI Projects"
   tar -czf trading-agent.tgz --exclude=.venv --exclude=node_modules trading-agent
   scp -i C:\path\to\ssh-key.key trading-agent.tgz ubuntu@<public-ip>:~
   ```

   The 1-minute history is several GB; this can take a while on home upload speeds.
3. Log in to the server and unpack:

   ```powershell
   ssh -i C:\path\to\ssh-key.key ubuntu@<public-ip>
   ```

   ```bash
   tar -xzf trading-agent.tgz
   ```

## 4. Install (5 min, on the server)

```bash
cd ~/trading-agent && bash deploy/install.sh
```

It prints a Tailscale link: open it on your phone or laptop to add the server to your private network. At the end it
prints your dashboard address, e.g. `https://trading-agent.tail1234.ts.net`. Then:

1. **Dashboard password:**

   ```bash
   cd /opt/trading-agent && sudo -u agent .venv/bin/python -m agent.cli set-password
   ```

2. **Uptime ping** (if you made the Healthchecks.io check): add its URL to `/opt/trading-agent/.env`. Open the file
   with `sudo nano /opt/trading-agent/.env` and add:

   ```
   HEALTHCHECK_URL=https://hc-ping.com/…
   ```

3. **Kite developer console** → your app → **Redirect URL**: `https://<your-name>.ts.net/api/kite/callback`.
4. **Start:**

   ```bash
   sudo systemctl start trading-agent
   ```

   Telegram should say *"Trading agent started on …"*.

## 5. Check (5 min)

- On your phone (Tailscale on), open the dashboard address and sign in with the password.
- Login screen → **Log in to Kite** → after Zerodha's page you land back on the dashboard, logged in.
- The Overview's jobs, Live prices and data look as they did on the laptop.
- **Don't start run.bat on the laptop again.** Two agents would each run the paper portfolios. To use the laptop
  copy for something else later, first set `kite.websocket: false` and keep it logged out.

## Day to day

| What | How |
|---|---|
| Morning Kite login | Tap the link in the 08:30 Telegram alert (or open the dashboard → Login). |
| Restart | `sudo systemctl restart trading-agent` (it also restarts by itself after a crash or reboot). |
| Logs | `sudo journalctl -u trading-agent -f`, or `/opt/trading-agent/runtime/logs/agent.log` |
| Update the code | Copy the new files to `~/trading-agent`, run `bash deploy/install.sh` again, then restart. Your data, .env and password are kept. |
| Server down? | Healthchecks.io messages you on Telegram after 10 minutes without a ping. When it comes back, Telegram says whether the last stop was clean. |
| OS updates | Automatic; a reboot, when one is needed, happens at 02:30. The agent starts by itself and catches up missed jobs after the Kite login. |

**Optional hardening once Tailscale works:** in the Oracle console → your VCN → Security List, remove the rule that
opens port 22 to everyone. You can still log in via Tailscale with `ssh ubuntu@<your-name>.ts.net`, after running
`sudo tailscale up --ssh` once.

**Before live trading:**
1. In Oracle (Networking → IP management), change the server's public IP to a **reserved** one, so it never changes.
2. Whitelist that IP in the Kite developer console. Exchange rules allow one change a week.
