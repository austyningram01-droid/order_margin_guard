import dashboard
import os, json
DB_PATH = os.environ.get("DB_PATH", getattr(dashboard, "DB_PATH", "margin_guard.db")).strip() or "margin_guard.db"
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs("config", exist_ok=True)
os.makedirs("logs", exist_ok=True)
if not os.path.exists("sku_cogs.json"):
    with open("sku_cogs.json", "w") as _f:
        json.dump({}, _f)
if not os.path.exists("settings.json"):
    with open("settings.json", "w") as _f:
        json.dump({
            "shopify_store_url": os.environ.get("SHOPIFY_STORE_URL", ""),
            "shopify_access_token": os.environ.get("SHOPIFY_ACCESS_TOKEN", ""),
            "shopify_shared_secret": os.environ.get("SHOPIFY_SHARED_SECRET", ""),
            "discord_webhook_url": os.environ.get("DISCORD_WEBHOOK_URL", "")
        }, _f)

import warnings
warnings.filterwarnings("ignore")

import json, csv, os, urllib3
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone

http = urllib3.PoolManager()
SETTINGS_FILE = "settings.json"
COGS_FILE = "sku_cogs.json"
LEDGER_FILE = "logs/daily_profit_ledger.csv"

with open(COGS_FILE, "r") as f:
    SKU_COSTS = json.load(f)

with open(SETTINGS_FILE, "r") as f:
    SETTINGS = json.load(f)


def evaluate_order_margin(order: dict) -> dict:
    rev = float(order.get("total_price", 0.0))
    disc = float(order.get("total_discounts", 0.0))
    codes = [d.get("code") for d in order.get("discount_codes", []) if d.get("code")]

    cogs = sum(
        SKU_COSTS.get(i.get("sku") or "DEFAULT", SKU_COSTS["DEFAULT"])["unit_cost"] * int(i.get("quantity", 1))
        for i in order.get("line_items", [])
    )
    ship = sum(
        SKU_COSTS.get(i.get("sku") or "DEFAULT", SKU_COSTS["DEFAULT"])["est_shipping"] * int(i.get("quantity", 1))
        for i in order.get("line_items", [])
    )
    fee = round((rev * 0.029) + 0.30, 2) if rev > 0 else 0.0
    profit = round(rev - cogs - ship - fee, 2)
    margin = round((profit / rev) * 100, 2) if rev > 0 else 0.0

    min_margin = float(SETTINGS.get("min_margin_pct", 15.0))
    reasons = []
    if profit <= 0:
        reasons.append(f"NEGATIVE_PROFIT (${profit})")
    elif margin < min_margin:
        reasons.append(f"LOW_MARGIN ({margin}%)")

    if len(codes) > 1:
        reasons.append(f"STACKED_CODES ({', '.join(codes)})")
    elif disc > 0 and (disc / (rev + disc)) >= 0.35:
        pct_off = round((disc / (rev + disc)) * 100, 1)
        reasons.append(f"HEAVY_DISCOUNT_LEAK ({codes[0] if codes else 'PROMO'}: {pct_off}% OFF)")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "order_id": str(order.get("id", "UNKNOWN")),
        "order_number": order.get("name", "#0000"),
        "gross_revenue": rev,
        "total_discounts": disc,
        "total_cogs": round(cogs, 2),
        "est_shipping_cost": round(ship, 2),
        "processing_fee": fee,
        "net_profit": profit,
        "margin_pct": margin,
        "action": "HOLD_FULFILLMENT" if reasons else "APPROVE",
        "hold_reasons": " | ".join(reasons) if reasons else "NONE"
    }


def notify_admin_discord(title: str, shop_domain: str, details: str = ""):
    admin_url = os.environ.get("ADMIN_DISCORD_WEBHOOK", "").strip()
    if not admin_url:
        return
    payload = {
        "username": "Order Margin Guard • Founder Telemetry",
        "embeds": [{
            "title": title,
            "color": 1096065,
            "fields": [
                {"name": "Store", "value": shop_domain or "Unknown", "inline": True},
                {"name": "Details", "value": details or "—", "inline": False}
            ]
        }]
    }
    try:
        http.request("POST", admin_url, body=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    except Exception as e:
        print(f"[Admin Telemetry Warning] {e}")


def hold_shopify_fulfillment(order_id: str, reason_notes: str) -> str:
    domain = SETTINGS.get("shopify_domain", "").replace("https://", "").strip("/")
    token = SETTINGS.get("shopify_access_token", "")
    try:
        fresh_tok, _ = dashboard.get_shop_token_and_plan(domain)
        if fresh_tok:
            token = fresh_tok
    except Exception:
        pass
    if not domain or not token:
        return "MISSING_SHOP_TOKEN"
    url = f"https://{domain}/admin/api/2026-01/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
        "User-Agent": "MarginGuardBot/1.0"
    }

    gid = order_id if str(order_id).startswith("gid://") else f"gid://shopify/Order/{order_id}"
    query_payload = {
        "query": """
        query($orderId: ID!) {
          order(id: $orderId) {
            fulfillmentOrders(first: 5) {
              edges { node { id status } }
            }
          }
        }
        """,
        "variables": {"orderId": gid}
    }

    resp = http.request("POST", url, body=json.dumps(query_payload).encode("utf-8"), headers=headers)
    data = json.loads(resp.data.decode("utf-8"))
    edges = data.get("data", {}).get("order", {}).get("fulfillmentOrders", {}).get("edges", [])

    if not edges:
        return "NO_FULFILLMENT_ORDER_FOUND"

    mutation = """
    mutation fulfillmentOrderHold($fulfillmentHold: FulfillmentOrderHoldInput!, $id: ID!) {
      fulfillmentOrderHold(fulfillmentHold: $fulfillmentHold, id: $id) {
        fulfillmentOrder { id status }
        userErrors { field message }
      }
    }
    """
    held_ids = []
    for edge in edges:
        fo_id = edge["node"]["id"]
        if edge["node"].get("status") == "ON_HOLD":
            held_ids.append(f"{fo_id.split('/')[-1]}(ON_HOLD)")
            continue
        mut_payload = {
            "query": mutation,
            "variables": {
                "id": fo_id,
                "fulfillmentHold": {
                    "reason": "OTHER",
                    "reasonNotes": f"Margin Guard Hold: {reason_notes}"
                }
            }
        }
        m_resp = http.request("POST", url, body=json.dumps(mut_payload).encode("utf-8"), headers=headers)
        m_data = json.loads(m_resp.data.decode("utf-8"))
        hold_res = m_data.get("data", {}).get("fulfillmentOrderHold", {})
        if not hold_res.get("userErrors") and hold_res.get("fulfillmentOrder"):
            held_ids.append(f"{fo_id.split('/')[-1]}(ON_HOLD)")

    return f"LIVE_HOLD_PLACED [{', '.join(held_ids)}]" if held_ids else "HOLD_API_ERROR"


def send_instant_alert(res: dict, hold_status: str):
    webhook_url = SETTINGS.get("alert_webhook_url", "")
    if not webhook_url: return
    msg = (
        f"🚨 **LIVE WEBHOOK MARGIN GUARD HOLD** 🚨\n"
        f"**Order:** `{res['order_number']}` (ID: `{res['order_id']}`)\n"
        f"**Gross Revenue:** ${res['gross_revenue']:.2f} | **Net Profit:** ${res['net_profit']:.2f} ({res['margin_pct']}%)\n"
        f"**Reasons:** `{res['hold_reasons']}`\n"
        f"**Shopify Status:** `{hold_status}`\n"
        f"🔗 https://admin.shopify.com/store/{SETTINGS.get('shopify_domain', 'ticktron').split('.')[0]}/orders/{res['order_id']}"
    )
    http.request("POST", webhook_url, body=json.dumps({"content": msg}).encode("utf-8"), headers={"Content-Type": "application/json"})


def log_to_ledger(row: dict):
    exists = os.path.isfile(LEDGER_FILE) and os.path.getsize(LEDGER_FILE) > 0
    with open(LEDGER_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists: writer.writeheader()
        writer.writerow(row)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        return self.do_GET()

    def do_GET(self):
        return dashboard.handle_get(self)

    def do_POST(self):
        if self.path.startswith("/api/session-auth"):
            auth_header = self.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"Missing Bearer token"}')
                return
            bearer = auth_header[7:].strip()
            try:
                import base64, sqlite3, time, urllib.request
                parts = bearer.split(".")
                if len(parts) >= 2:
                    pad = "=" * ((4 - len(parts[1]) % 4) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
                    tok_shop = claims.get("dest", "").replace("https://", "").strip("/").lower()
                    if tok_shop and not tok_shop.endswith(".myshopify.com"):
                        tok_shop = f"{tok_shop}.myshopify.com"
                    cid = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
                    csec = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
                    if not cid or not csec:
                        try:
                            st = json.load(open("settings.json"))
                            cid = cid or st.get("shopify_client_id", "").strip()
                            csec = csec or st.get("shopify_client_secret", "").strip()
                        except Exception:
                            pass
                    if tok_shop and cid and csec:
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute("CREATE TABLE IF NOT EXISTS merchants (shop_domain TEXT PRIMARY KEY, min_margin_pct REAL DEFAULT 15.0, alert_webhook_url TEXT DEFAULT '', access_token TEXT DEFAULT '', plan_status TEXT DEFAULT 'FREE_TRIAL', refresh_token TEXT DEFAULT '', expires_at INTEGER DEFAULT 0)")
                        row = c.execute("SELECT access_token, expires_at FROM merchants WHERE shop_domain = ?", (tok_shop,)).fetchone()
                        has_valid_expiring = bool(row and row[0] and row[1] and int(row[1]) > int(time.time()) + 60)
                        if not has_valid_expiring:
                            ex_req = urllib.request.Request(
                                f"https://{tok_shop}/admin/oauth/access_token",
                                data=json.dumps({
                                    "client_id": client_id if "client_id" in locals() else cid,
                                    "client_secret": csec,
                                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                                    "subject_token": bearer,
                                    "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
                                    "requested_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
                                    "expiring": "1"
                                }).encode("utf-8"),
                                method="POST",
                                headers={"Content-Type": "application/json", "Accept": "application/json"}
                            )
                            with urllib.request.urlopen(ex_req, timeout=10) as ex_resp:
                                ex_data = json.loads(ex_resp.read().decode("utf-8"))
                                ex_tok = ex_data.get("access_token", "")
                                ex_ref = ex_data.get("refresh_token", "")
                                ex_exp_in = int(ex_data.get("expires_in", 0) or 0)
                                ex_exp_at = (int(time.time()) + ex_exp_in) if ex_exp_in > 0 else 0
                                if ex_tok:
                                    is_new_shop = not bool(row)
                                    c.execute("INSERT OR IGNORE INTO merchants (shop_domain, min_margin_pct, alert_webhook_url, access_token, plan_status) VALUES (?, 15.0, '', ?, 'FREE_TRIAL')", (tok_shop, ex_tok))
                                    c.execute("UPDATE merchants SET access_token = ?, refresh_token = ?, expires_at = ? WHERE shop_domain = ?", (ex_tok, ex_ref, ex_exp_at, tok_shop))
                                    conn.commit()
                                    if is_new_shop:
                                        notify_admin_discord("🚀 New App Install!", tok_shop, "Merchant completed OAuth token exchange.")
                                    print(f"[✓] Exchanged idToken for expiring=1 offline token for {tok_shop} (expires_in={ex_exp_in}s)")
                        conn.close()
            except Exception as ex_err:
                print(f"[!] Session token exchange note: {ex_err}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"authenticated"}')
            return
        if self.path.startswith("/api/"):
            return dashboard.handle_post(self)
        topic = self.headers.get("X-Shopify-Topic", "")
        if topic == "app/uninstalled":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            hmac_header = self.headers.get("X-Shopify-Hmac-Sha256", "")
            if not dashboard.verify_shopify_hmac(body, hmac_header):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"Unauthorized HMAC")
                return
            uninstalled_shop = dashboard.normalize_shop(self.headers.get("X-Shopify-Shop-Domain", ""))
            if uninstalled_shop:
                dashboard.init_db()
                conn = dashboard.sqlite3.connect(dashboard.DB_PATH)
                c = conn.cursor()
                c.execute("UPDATE merchants SET access_token = '', plan_status = 'FREE_TRIAL' WHERE shop_domain = ?", (uninstalled_shop,))
                conn.commit()
                notify_admin_discord("⚠️ App Uninstalled", uninstalled_shop, "Merchant uninstalled Order Margin Guard.")
                conn.close()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return
        if self.path.startswith("/webhooks/compliance") or topic in ("customers/data_request", "customers/redact", "shop/redact"):
            return dashboard.handle_compliance_webhook(self)
        dashboard.sync_state(self.headers.get("X-Shopify-Shop-Domain"))
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        hmac_header = self.headers.get("X-Shopify-Hmac-Sha256", "")
        if not dashboard.verify_shopify_hmac(body, hmac_header):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized HMAC")
            return
        
        try:
            order_data = json.loads(body.decode('utf-8'))
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            return

        print(f"\n[⚡] INCOMING SHOPIFY WEBHOOK: Order {order_data.get('name', '#0000')} received!")
        res = evaluate_order_margin(order_data)
        
        if res["action"] == "HOLD_FULFILLMENT":
            print(f"    [!] Margin anomaly detected! Placing fulfillment hold...")
            incoming_shop = self.headers.get("X-Shopify-Shop-Domain", "")
            res["shopify_hold_status"] = hold_shopify_fulfillment(res["order_id"], res["hold_reasons"])
            send_instant_alert(res, res["shopify_hold_status"])
            notify_admin_discord("💰 Low-Margin Order Caught!", incoming_shop, f"Order {res['order_number']} | Net: ${res['net_profit']:.2f} ({res['margin_pct']}%) | Status: {res['shopify_hold_status']}")
        else:
            print(f"    [✓] Order profitable and clean. Approved.")
            res["shopify_hold_status"] = "NOT_REQUIRED"
            res["alert_status"] = "NOT_REQUIRED"

        log_to_ledger(res)
        dashboard.record_order(self.headers.get("X-Shopify-Shop-Domain"), order_data, res)
        
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return  # Suppress default noisy HTTP access logs


if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', PORT), WebhookHandler)
    print(f"\n[🟢] Margin Guard Webhook Listener active on port {PORT}...")
    print("     Ready to receive live checkout events from Shopify.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] Shutting down server...")
        server.server_close()

