import json, os, urllib3
from urllib.parse import urlencode

http = urllib3.PoolManager()
SETTINGS_FILE = "config/settings.json"
SHOP = "ticktron.myshopify.com"

print(f"\n=== DIRECT SHOPIFY AUTH FOR {SHOP} (NO BROWSER NEEDED) ===")
client_id = input("1. Paste your Shopify App Client ID: ").strip()
client_secret = input("2. Paste your Shopify App Client Secret: ").strip()

token_url = f"https://{SHOP}/admin/oauth/access_token"
body = urlencode({
    "grant_type": "client_credentials",
    "client_id": client_id,
    "client_secret": client_secret
})

resp = http.request(
    "POST",
    token_url,
    body=body,
    headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "MarginGuardBot/1.0"
    }
)

raw_text = resp.data.decode("utf-8", errors="ignore")

if resp.status == 200:
    data = json.loads(raw_text)
    token = data.get("access_token", "")
    with open(SETTINGS_FILE, "r") as f:
        settings = json.load(f)
    settings["shopify_domain"] = SHOP
    settings["shopify_client_id"] = client_id
    settings["shopify_client_secret"] = client_secret
    settings["shopify_access_token"] = token
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
    print(f"\n[✓] SUCCESS! Access token generated and saved to {SETTINGS_FILE}!")
    print(f"    Granted Scopes: {data.get('scope', 'default')}\n")
    os.system("python3 margin_guard_engine.py")
else:
    print(f"\n[X] HTTP {resp.status} Error:")
    print(raw_text[:400])
