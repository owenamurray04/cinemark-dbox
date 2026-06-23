# One-time setup: US residential proxy + GitHub Actions

The scraper runs itself on GitHub Actions, but two things need a one-time setup
because Cinemark blocks datacenter IPs (including GitHub's runners).

## 1. US residential proxy (`CINEMARK_PROXY`)

Reuse the existing **DataImpulse** account — just point it at **US** exits.

- Username form for US: append `__cr.us` to your DataImpulse username.
- Proxy string:
  ```
  http://USER__cr.us:PASSWORD@gw.dataimpulse.com:823
  ```

Add it as a repo secret:

1. Repo → **Settings → Secrets and variables → Actions → New repository secret**
2. Name: `CINEMARK_PROXY`
3. Value: the proxy string above.

## 2. Self-chaining token (`DISPATCH_PAT`)

The live loop re-triggers itself with a `repository_dispatch`, which needs a token.

1. GitHub → **Settings → Developer settings → Fine-grained tokens → Generate new token**
2. Repository access: **Only select repositories** → this repo.
3. Permissions: **Contents → Read and write**.
4. Copy the token, then add it as a repo secret named `DISPATCH_PAT`.

## 3. Turn on GitHub Pages (for the dashboard)

- Repo → **Settings → Pages** → Source: **Deploy from a branch** → branch `main`
  (or `master`), folder **/ (root)** or **/dashboard**.
- The dashboard is at `dashboard/index.html` and reads `dashboard/cinemark_data.json`
  (same folder), so serving the `dashboard/` folder works directly.

## 4. Start the loop

- Repo → **Actions → "Cinemark D-BOX live loop" → Run workflow**.
- It keeps itself going. To stop it: Actions → the workflow → **··· → Disable
  workflow** (cancelling a single run won't stop it — it relaunches itself).

## Local run (no Actions)

```bash
export CINEMARK_PROXY="http://USER__cr.us:PASSWORD@gw.dataimpulse.com:823"
python3 cinemark_scraper.py discover --date "$(date +%-m/%-d/%Y)"
python3 cinemark_scraper.py measure  --date "$(date +%-m/%-d/%Y)"
```

If a local run still gets blocked, open cinemark.com in your browser, DevTools →
Network, right-click any `www.cinemark.com` request → Copy → **Copy as cURL**, and
paste the whole thing into a file named `cinemark_cookie.txt` in this folder.
