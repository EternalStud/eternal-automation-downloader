# 🤝 Contributing to EternalAutomationBot

> **Development guidelines, architectural standards, and testing procedures for EternalAutomationBot.**

---

## 🛠️ Development Workflow

1. **Branching**:
   ```bash
   git checkout -b feature/my-new-feature
   ```
2. **Coding Standards**:
   - Write clean, modular Python 3 code with defensive null-safety on all Telegram payload dictionaries.
   - Use `TG_SESSION` for all outbound Telegram API requests to preserve connection pooling.
   - Ensure all media processing tasks run within `DOWNLOAD_EXECUTOR` to prevent blocking the polling dispatcher.
3. **Local Syntax & Compilation Check**:
   ```bash
   python3 -m py_compile main.py
   ```
4. **Commit & Push**:
   ```bash
   git add .
   git commit -m "Descriptive summary of changes"
   git push origin feature/my-new-feature
   ```

---

## 💡 Common Extension Patterns

### 1. Adding a New Command
To add a new command (e.g. `/stats` or `/info`), edit the command router in [`handlers.py`](file:///home/eternal/eternal-automation-bot/handlers.py):

```python
elif text == "/mycommand":
    reply = "✨ Custom response message here!"
```

### 2. Adding a Channel Moderation Keyword
To delete messages containing additional spam patterns in channels, add items to `DELETE_KEYWORDS` in [`handlers.py`](file:///home/eternal/eternal-automation-bot/handlers.py):

```python
DELETE_KEYWORDS = [
    "share price live updates",
    "exclusive crypto signals",
    "join now for 100x"
]
```

### 3. Adding a New Platform Extractor
To label new platforms in progress messages, update `detect_platform(url)` in [`downloader.py`](file:///home/eternal/eternal-automation-bot/downloader.py):

```python
if "pinterest.com" in u or "pin.it" in u:
    return "Pinterest"
```

---

## 🧪 Testing Checklist

Before opening a pull request or deploying to the phone server:
- [ ] Code compiles cleanly with `python3 -m py_compile main.py handlers.py downloader.py queue_manager.py`.
- [ ] Test suite passes cleanly: `python3 -m unittest discover -s tests -p "test_*.py"`.
- [ ] Subscription access control (`is_user_active`) is preserved on all media download paths.
- [ ] File size check (`MAX_TELEGRAM_SIZE_BYTES`) is enforced.
- [ ] Temporary files in `downloads/` are deleted upon completion or failure.
- [ ] Inline keyboard callback patterns remain strictly protected by `OWNER_ID` verification.
- [ ] Lifecycle and boot scripts pass `--dry-run` checks without errors.

