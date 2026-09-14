# 🛠️ Setup & Administrator Guide

> **Comprehensive guide to setting up, configuring, and managing EternalAutomationBot on your Samsung Galaxy M31 server, configuring payment options, managing user bans, monitoring platform cookie health, and moderating Telegram channels.**

---

## 📋 Table of Contents

1. [Telegram Bot & BotFather Setup](#1-telegram-bot--botfather-setup)
2. [Phone Server Configuration (`.env`)](#2-phone-server-configuration-env)
3. [Managing Subscribers & Pricing](#3-managing-subscribers--pricing)
4. [Admin User Ban Management (Anti-Spam)](#4-admin-user-ban-management-anti-spam)
5. [Autonomous Cookie & Session Diagnostics](#5-autonomous-cookie--session-diagnostics)
6. [In-Bot Payment Screenshot Approvals](#6-in-bot-payment-screenshot-approvals)
7. [Channel Auto-Cleanup Moderation](#7-channel-auto-cleanup-moderation)
8. [24/7 Operations with `tmux` & Watchdog](#8-247-operations-with-tmux--watchdog)
9. [Troubleshooting & Diagnostics](#9-troubleshooting--diagnostics)

---

## 1. Telegram Bot & BotFather Setup

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to name your bot and choose a username (e.g. `EternalAutomationBot`).
3. BotFather will provide your **Bot Token** (e.g. `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).
4. **Set Bot Commands in BotFather**:
   - Send `/setcommands` to BotFather.
   - Select your bot and paste:
     ```text
     start - Welcome menu & subscription status
     help - View supported platforms & commands
     whoami - Check active plan, remaining days & free quota
     status - Server health & subscriber metrics
     ping - Latency & server test
     support - Send direct support ticket to Admin
     refer - Earn +7 free days by inviting friends
     renew - Instant UPI QR code for ₹20/month subscription
     subs - Subscriber overview (Admin)
     ban - Ban user from bot access (Admin)
     unban - Restore user access (Admin)
     banned - View banned users list (Admin)
     cookies - Cookie diagnostics & download (Admin)
     ```

---

## 2. Phone Server Configuration (`.env`)

On your Samsung M31 smartphone (or VPS/PC), create a `.env` file in the project folder:

```bash
cd ~/eternal-automation-bot
nano .env
```

Paste the following configuration:

```ini
# Telegram Bot Token from @BotFather
BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ

# Admin Handle displayed to users for support
ADMIN_HANDLE=@YourUsername

# Your UPI ID for accepting ₹20/month payments
UPI_ID=yourname@bank

# Optional Netscape cookies for private/restricted media
COOKIES_TXT=
```
Save with `Ctrl + O`, `Enter`, `Ctrl + X`.

---

## 3. Managing Subscribers & Pricing

The default subscription price is set to **₹20 / Month** in `config.py`.

### Granting / Renewing Subscriptions from Telegram:
As the Owner, send commands directly to `@EternalAutomationBot`:

- **Grant 30 Days**:
  ```text
  /addsub 8851748974 30 Rahul
  ```
- **Grant Custom Days**:
  ```text
  /addsub 8851748974 60 Rahul
  ```
- **Revoke Access**:
  ```text
  /deluser 8851748974
  ```
- **View All Members**:
  ```text
  /subs
  ```

---

## 4. Admin User Ban Management (Anti-Spam)

Protect your Samsung phone server from abuse and malicious flooding:

- **Ban a Spammer**:
  ```text
  /ban 8851748974 Flooding requests repeatedly
  ```
  *Bans the user permanently, prevents all downloading, and drops requests at zero CPU cost.*

- **Unban a User**:
  ```text
  /unban 8851748974
  ```
  *Restores the user's account and notifies them.*

- **View All Banned Users**:
  ```text
  /banned
  ```

- **1-Tap Inline Quick-Ban**:
  *When a user sends a `/support <message>` or triggers an alert, tap the **`[ 🚫 Ban User ]`** button directly in your chat.*

---

## 5. Autonomous Cookie & Session Diagnostics

- **Autonomous Visitor Handshakes**: The bot autonomously pulls and refreshes guest visitor tokens for **Facebook, Instagram, YouTube, and Twitter/X** every 12 hours.
- **Check Live Session Health**:
  ```text
  /checkcookies
  ```
  *Probes Facebook and Instagram live endpoints to verify session cookies and sends you the current `cookies.txt` file.*
- **Update Cookies (Zero-SSH)**:
  *Simply drag and drop or send your new `cookies.txt` file directly as a document to the bot in Telegram!*

---

## 6. In-Bot Payment Screenshot Approvals

When a new user wants to subscribe:
1. They ping `@EternalAutomationBot` and receive the **Dynamic UPI QR Code** pre-filled with `yourname@bank` and amount `₹20`.
2. They scan and pay via **GPay, PhonePe, Paytm, or BHIM**.
3. They send the payment screenshot **directly into the bot chat**.
4. The bot forwards the screenshot to your private chat with:
   - `[ ✅ Approve (30 Days) ]`
   - `[ ❌ Reject ]`
5. Tap **Approve** ➔ The bot automatically activates their account for 30 days and sends them a confirmation!

---

## 7. Channel Auto-Cleanup Moderation

To automatically delete spam phrases like `"share price live updates"` from your channel (e.g. `@YourChannel`):

1. Open your Telegram Channel settings.
2. Go to **Administrators** ➔ **Add Administrator** ➔ Select your bot.
3. Grant **Delete Messages** permission.
4. The bot will automatically intercept and delete any matching post in real-time.

---

## 8. 24/7 Operations with `tmux` & Watchdog

To keep the bot running 24/7 with automatic crash recovery:

### Start Watchdog Session:
```bash
cd ~/eternal-automation-bot
tmux new-session -d -s bot 'bash start.sh'
```

### View Live Real-Time Logs:
```bash
tmux attach -t bot
```
*(Press `Ctrl + B` then `D` to detach safely).*

### Restart Bot:
```bash
tmux send-keys -t bot C-c
sleep 1
tmux send-keys -t bot "bash start.sh" C-m
```

---

## 9. Termux:Boot & Scheduled Weekly Maintenance

To make your phone server reboot weekly and auto-recover all 3 bots without human intervention:

### 1. Enable Samsung One UI Auto-Restart:
1. Open **Settings** ➔ **Battery and Device Care** (or **Device Care**).
2. Tap **Auto Optimization** ➔ **Auto Restart**.
3. Enable **Restart on schedule** ➔ Select **Sunday at 3:00 AM**.

### 2. Pre-Reboot Cron Maintenance:
The bot suite automatically registers a cron job running at **02:55 AM** (5 minutes before reboot) via [`scripts/setup_cron.sh`](file:///Volumes/Eternal%20T7/PROJECT/eternal-automation-bot/scripts/setup_cron.sh):
- Broadcasts clean shutdown signals to all 3 bots (`bot`, `feed-bot`, `heartcast`).
- Validates database integrity and flushes all state.
- Purges temporary downloads and clears lock files.

### 3. Termux:Boot Auto-Startup Suite:
- Place [`scripts/termux_boot.sh`](file:///Volumes/Eternal%20T7/PROJECT/eternal-automation-bot/scripts/termux_boot.sh) in `~/.termux/boot/01-phone-server-boot.sh`.
- Install the **Termux:Boot** companion app from F-Droid.
- On phone boot, the script will:
  1. Acquire `termux-wake-lock`.
  2. Start `crond` supervisor.
  3. Wait for Wi-Fi/4G connectivity.
  4. Launch all 3 bots in `tmux` sessions (`bot`, `feed-bot`, `heartcast`).
  5. Dispatch a Telegram notification with server telemetry to Admin.

---

## 10. Troubleshooting & Diagnostics

| Issue | Cause | Solution |
|---|---|---|
| **Bot not responding** | Process stopped or token missing | Verify with `pgrep -fa main.py` or check `tmux attach -t bot`. |
| **"Private / Restricted Video"** | Media requires platform login | Send a fresh `cookies.txt` file directly into your bot chat. |
| **User not authorized** | Subscription expired or trial finished | Run `/addsub <user_id> 30` or tell user to type `/renew`. |
| **User sending spam** | User flooding link requests | Run `/ban <user_id>` or tap `[ 🚫 Ban User ]` in support ticket. |
| **Cron not running** | crond daemon inactive | Run `crond` in Termux or re-run `bash scripts/setup_cron.sh`. |


