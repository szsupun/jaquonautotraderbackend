# How to Get Your PocketOption SSID

## What is SSID?
The SSID is the authentication message that PocketOption uses to connect via WebSocket. You need it to let the bot trade on your account. Each trader pastes their own — it's never shared between users.

---

## Fast way: a browser extension that grabs it for you

A one-time install that then captures your SSID automatically on every visit to pocketoption.com — no DevTools, no manual copying.

(A bookmarklet was tried first but doesn't actually work for this: it can only patch the page *after* it's already loaded, and needs a reload to catch the connection — but reloading wipes out anything a bookmarklet installed, so it could never actually see the auth message. An extension's content script installs itself *before* the page's own code runs, on every load, which is what makes automatic capture actually possible.)

Two versions, same tool:

- **Chrome / Edge**: `ssid-grabber-extension/README.md` — "Load unpacked" in `chrome://extensions`, no store review, under a minute, but each user has to enable Developer mode
- **Firefox**: `ssid-grabber-extension-firefox/README.md` — gets signed by Mozilla for private distribution (free, automated, no public listing needed), then installs like a normal extension for anyone — no Developer mode required at all

Once installed: go to pocketoption.com, log in — a small overlay pops up automatically with your SSID pre-selected. Click **Copy**, then paste it into the Mini App's Settings tab, same as below.

---

## Manual way (DevTools)

### 1. Open PocketOption
- Go to [pocketoption.com](https://pocketoption.com) and **log in** to your account
- Make sure you're on the **trading page** (the chart screen)

### 2. Open Browser DevTools
- Press **F12** (or `Ctrl+Shift+I` on Windows / `Cmd+Option+I` on Mac)
- This opens the Developer Tools panel

### 3. Go to Network Tab
- Click the **"Network"** tab at the top of DevTools
- Then click the **"WS"** filter (WebSocket) to show only WebSocket connections

### 4. Refresh the Page
- Press **F5** to reload the page
- You'll see WebSocket connections appear in the list

### 5. Find the Auth Message
- Click on the WebSocket connection (usually the one with the longest name or `wss://...`)
- Click the **"Messages"** tab
- Look for a message that starts with: `42["auth",{...}]`
- It looks like this:

```
42["auth",{"session":"abc123xyz...","isDemo":1,"uid":12345678,"platform":2,"isFastHistory":true,"isOptimized":true}]
```

### 6. Copy the Full Message
- **Right-click** on that message → **Copy message**
- Or manually select and copy the entire line starting from `42[` to the closing `]`

### 7. Paste it into the Mini App
- Open the Mini App and go to the **Settings** tab
- Switch the account-mode toggle to **Demo** or **Real** (matching the `isDemo` value in the SSID you copied)
- Paste the whole `42["auth",...]` string into the SSID field and save
- The app checks the SSID's own `isDemo` flag against the field you're pasting into — if they don't match, it's rejected with a clear error rather than silently filed under the wrong mode

---

## Important Notes

| Setting | Value | Meaning |
|---------|-------|---------|
| `isDemo` | `1` | Demo account (fake money) |
| `isDemo` | `0` | **Real account (real money!)** |

- ⚠️ **Start with demo (`isDemo: 1`)** to test the bot first!
- 🔄 The SSID **expires** after some time — if the bot can't connect, get a fresh one the same way
- 🔒 **Never share** your SSID with anyone — it gives full access to your account
- Real-money trading additionally requires an admin-granted subscription — pasting a real SSID before that's granted will be rejected even if the SSID itself is valid

---

## Quick Reference (Chrome)

```
F12 → Network → WS → Refresh page → Click connection → Messages → Find 42["auth",...] → Copy → Paste into Mini App Settings
```
