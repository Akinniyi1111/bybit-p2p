#!/usr/bin/env python3
"""
Bybit P2P Telegram Bot (background worker style)

Environment variables (set in Render or locally):
- TELEGRAM_TOKEN
- BYBIT_API_KEY
- BYBIT_API_SECRET
- BYBIT_TESTNET  (optional, "true" or "false")
- ADMIN_TELEGRAM_ID (optional; default uses 1378825382)

Run: python main.py
"""
import os
import json
import time
import threading
import logging
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# Try to import SDK
try:
    from bybit_p2p import P2P
except Exception as e:
    P2P = None
    logging.warning("bybit_p2p SDK not installed or failed to import: %s", e)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

STATE_FILE = "state.json"
POLL_INTERVAL = 5  # seconds (tune as needed; avoid too aggressive polling)

# Default config according to your request
DEFAULT_STATE = {
    "market": {"coin": "USDT", "fiat": "NGN", "side": "BUY", "watch_side": "SELL"},
    "price_range": {"min": 1400.0, "max": 1455.0},
    "min_buy": 10000,
    "max_buy": 1000000,
    "auto_start": False,
    "bot_running": False,
    "admin_id": int(os.getenv("ADMIN_TELEGRAM_ID", "1378825382")),
    "history": []
}

# Helpers to load/save state
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                st = json.load(f)
            # ensure keys exist
            for k, v in DEFAULT_STATE.items():
                if k not in st:
                    st[k] = v
            return st
        except Exception as e:
            logging.exception("Failed load state: %s", e)
    return DEFAULT_STATE.copy()

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, default=str, indent=2)
    except Exception as e:
        logging.exception("Failed save state: %s", e)

# prune history older than 10 days
def prune_history(state):
    cutoff = datetime.utcnow() - timedelta(days=10)
    new_hist = []
    for item in state.get("history", []):
        try:
            ts = datetime.fromisoformat(item["timestamp"])
        except Exception:
            ts = datetime.utcnow()
        if ts >= cutoff:
            new_hist.append(item)
    state["history"] = new_hist

# Build Bybit client (reads env)
def build_bybit_client():
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    testnet_env = os.getenv("BYBIT_TESTNET", "true").lower() in ("1", "true", "yes")
    if not P2P:
        logging.error("bybit_p2p SDK unavailable; install bybit-p2p in requirements.")
        return None
    try:
        client = P2P(testnet=testnet_env, api_key=api_key, api_secret=api_secret)
        logging.info("Bybit P2P client created (testnet=%s)", testnet_env)
        return client
    except Exception as e:
        logging.exception("Failed create Bybit client: %s", e)
        return None

class BotEngine:
    def __init__(self, app, state):
        self.app = app
        self.state = state
        self.client = build_bybit_client()
        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self.thread = threading.Thread(target=self.worker_loop, daemon=True)
        self.seen_ads = set()  # avoid duplicate immediate orders

    def start(self):
        if not self.thread.is_alive():
            self._stop_event.clear()
            self.thread = threading.Thread(target=self.worker_loop, daemon=True)
            self.thread.start()
            logging.info("Worker thread started")
        self.state["bot_running"] = True
        save_state(self.state)

    def stop(self):
        self._stop_event.set()
        self.state["bot_running"] = False
        save_state(self.state)
        logging.info("Worker stop requested")

    async def notify_admin(self, text, buttons=None, parse_mode=ParseMode.HTML):
        admin_id = self.state.get("admin_id")
        try:
            if buttons:
                await self.app.bot.send_message(admin_id, text, reply_markup=buttons, parse_mode=parse_mode)
            else:
                await self.app.bot.send_message(admin_id, text, parse_mode=parse_mode)
        except Exception as e:
            logging.exception("Failed to notify admin: %s", e)

    def get_online_ads(self):
        """
        Call SDK to get online ads for the configured market.
        Returns list of ads (SDK-specific objects). We try common wrapper names.
        """
        if not self.client:
            logging.error("Bybit client not configured")
            return []
        try:
            for method_name in ("get_online_ads", "get_ads_list", "get_ads", "get_ads_online"):
                method = getattr(self.client, method_name, None)
                if method:
                    try:
                        params = dict(coin=self.state["market"]["coin"],
                                      fiat=self.state["market"]["fiat"],
                                      side=self.state["market"]["watch_side"],
                                      page=1, limit=50)
                        resp = method(**params)
                        if isinstance(resp, dict):
                            items = resp.get("items") or resp.get("data") or resp.get("ads") or resp.get("result")
                            if isinstance(items, list):
                                return items
                        elif isinstance(resp, list):
                            return resp
                    except Exception as e:
                        logging.debug("Method %s called but failed: %s", method_name, e)
            logging.error("No ads method matched or all methods failed")
        except Exception as e:
            logging.exception("Error retrieving online ads: %s", e)
        return []

    def create_buy_order(self, ad, amount_fiat):
        """
        Try to create a buy order using SDK. SDK wrappers differ; try common call names.
        Return order dict or None.
        """
        if not self.client:
            logging.error("Bybit client not configured for create order")
            return None
        ad_id = ad.get("adId") or ad.get("advertisementId") or ad.get("id") or ad.get("ad_id")
        if not ad_id:
            logging.error("ad object missing id: %s", ad)
            return None
        params = {"advertisementId": ad_id, "amount": str(amount_fiat)}
        for method_name in ("create_order", "create_buy_order", "create_order_v5", "create_order_v1",
                            "post_order", "place_order"):
            method = getattr(self.client, method_name, None)
            if not method:
                continue
            try:
                resp = method(**params)
                logging.info("create order via %s success: %s", method_name, resp)
                return resp
            except Exception as e:
                logging.debug("create order method %s failed: %s", method_name, e)
        logging.error("No create order method succeeded")
        return None

    def worker_loop(self):
        logging.info("Worker loop entering main cycle")
        while not self._stop_event.is_set():
            try:
                if not self.state.get("bot_running"):
                    time.sleep(POLL_INTERVAL)
                    continue

                prune_history(self.state)

                ads = self.get_online_ads()
                if not ads:
                    time.sleep(POLL_INTERVAL)
                    continue

                for ad in ads:
                    try:
                        price_str = ad.get("price") or ad.get("unitPrice") or ad.get("rate")
                        if price_str is None:
                            continue
                        price = float(price_str)
                        ad_id = ad.get("adId") or ad.get("advertisementId") or ad.get("id") or str(price) + "_" + str(ad.get("sellerId", ""))
                        if ad_id in self.seen_ads:
                            continue

                        pmin = float(self.state["price_range"]["min"])
                        pmax = float(self.state["price_range"]["max"])
                        if pmin <= price <= pmax:
                            amount_fiat = int(self.state.get("min_buy", 10000))
                            ad_min = float(ad.get("minLimit") or ad.get("min") or 0)
                            ad_max = float(ad.get("maxLimit") or ad.get("max") or 1e18)
                            if amount_fiat < ad_min:
                                amount_fiat = int(ad_min)
                            if amount_fiat > ad_max:
                                logging.info("Ad %s cannot satisfy amount %s > ad_max %s", ad_id, amount_fiat, ad_max)
                                continue

                            logging.info("Attempting to create buy order for ad %s at price %s amount %s", ad_id, price, amount_fiat)
                            resp = self.create_buy_order(ad, amount_fiat)

                            ts = datetime.utcnow().isoformat()
                            record = {
                                "order_response": resp,
                                "ad": {"id": ad_id, "price": price, "raw": ad},
                                "amount": amount_fiat,
                                "timestamp": ts
                            }
                            self.state.setdefault("history", []).insert(0, record)
                            prune_history(self.state)
                            save_state(self.state)

                            order_id = None
                            if isinstance(resp, dict):
                                order_id = resp.get("orderId") or (resp.get("data") or {}).get("orderId")
                            text = (
                                f"🔥 <b>New Buy Order Created</b>\n"
                                f"Coin: {self.state['market']['coin']} / {self.state['market']['fiat']}\n"
                                f"Ad ID: {ad_id}\n"
                                f"Price: {price}\n"
                                f"Amount (fiat): {amount_fiat}\n"
                                f"Order ID: {order_id or 'N/A'}\n\n"
                                f"Please pay the seller outside the platform and click <b>I Have Paid</b> when done."
                            )
                            buttons = InlineKeyboardMarkup([
                                [InlineKeyboardButton("✅ I Have Paid", callback_data=f"markpaid:{order_id or 'none'}")],
                                [InlineKeyboardButton("🔍 View Ad", callback_data=f"viewad:{ad_id}")],
                            ])
                            try:
                                # schedule a coroutine on the application event loop
                                self.app.create_task(self.notify_admin(text, buttons=buttons))
                            except Exception as e:
                                logging.exception("Failed schedule notify task: %s", e)

                            self.seen_ads.add(ad_id)
                            if len(self.seen_ads) > 1000:
                                try:
                                    self.seen_ads.pop()
                                except Exception:
                                    # set.pop() may raise on empty; ignore
                                    pass
                    except Exception as e:
                        logging.exception("Error processing ad: %s", e)

                time.sleep(POLL_INTERVAL)
            except Exception as outer:
                logging.exception("Worker loop outer error: %s", outer)
                time.sleep(5)
        logging.info("Worker loop exiting")

# Telegram command handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.bot_data["state"]
    kb = [
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("▶️ Start Bot", callback_data="start_bot"),
         InlineKeyboardButton("⏹ Stop Bot", callback_data="stop_bot")],
        [InlineKeyboardButton("📜 History", callback_data="history")],
        [InlineKeyboardButton("ℹ️ Status", callback_data="status")]
    ]
    await update.message.reply_text("Bybit P2P Worker Bot — Main Menu", reply_markup=InlineKeyboardMarkup(kb))

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    state = context.bot_data["state"]
    engine: BotEngine = context.bot_data["engine"]

    if data == "settings":
        pr = state["price_range"]
        text = (
            f"Current settings:\n"
            f"Price range: {pr['min']} - {pr['max']}\n"
            f"Min buy: {state['min_buy']}\n"
            f"Max buy: {state['max_buy']}\n"
            f"Auto-start: {state['auto_start']}\n"
            f"Bot running: {state['bot_running']}\n\n"
            "Choose an action:"
        )
        kb = [
            [InlineKeyboardButton("Set Price Range", callback_data="set_range")],
            [InlineKeyboardButton("Set Min Buy", callback_data="set_min_buy"),
             InlineKeyboardButton("Set Max Buy", callback_data="set_max_buy")],
            [InlineKeyboardButton("Toggle Auto-Start", callback_data="toggle_autostart")],
            [InlineKeyboardButton("Back", callback_data="back_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "start_bot":
        engine.start()
        await query.edit_message_text("Crawler started ✅")
        return

    if data == "stop_bot":
        engine.stop()
        await query.edit_message_text("Crawler stopped ⏹")
        return

    if data == "history":
        hist = state.get("history", [])[:10]
        if not hist:
            await query.edit_message_text("No history yet.")
            return
        msgs = []
        for idx, item in enumerate(hist[:10], 1):
            ts = item.get("timestamp")
            ad = item.get("ad", {})
            msgs.append(f"{idx}. Ad {ad.get('id')} price {ad.get('price')} amount {item.get('amount')} time {ts}")
        await query.edit_message_text("\n".join(msgs))
        return

    if data == "status":
        pr = state["price_range"]
        text = (
            f"Status:\nBot running: {state['bot_running']}\n"
            f"Price range: {pr['min']} - {pr['max']}\nMin buy: {state['min_buy']}\nMax buy: {state['max_buy']}"
        )
        await query.edit_message_text(text)
        return

    if data == "set_range":
        await query.edit_message_text("Send the price range in the format: min-max (e.g. 1400-1455)")
        return

    if data == "set_min_buy":
        await query.edit_message_text("Send the minimum buy amount in fiat (e.g. 10000)")
        return

    if data == "set_max_buy":
        await query.edit_message_text("Send the maximum buy amount in fiat (e.g. 1000000)")
        return

    if data == "toggle_autostart":
        state["auto_start"] = not state.get("auto_start", False)
        save_state(state)
        await query.edit_message_text(f"Auto-start set to {state['auto_start']}")
        return

    if data.startswith("markpaid:"):
        order_id = data.split(":", 1)[1]
        if not engine.client:
            await query.edit_message_text("Bybit client not configured; cannot mark paid.")
            return
        try:
            success = False
            for method_name in ("mark_as_paid", "markOrderPaid", "order_pay", "mark_paid"):
                method = getattr(engine.client, method_name, None)
                if method:
                    try:
                        # vary argument style to match possible SDK variants
                        try:
                            res = method(order_id=order_id)
                        except TypeError:
                            try:
                                res = method(orderId=order_id)
                            except TypeError:
                                res = method(order_id)
                        logging.info("mark paid via %s -> %s", method_name, res)
                        success = True
                        break
                    except Exception as e:
                        logging.debug("mark paid %s failed: %s", method_name, e)
            if success:
                await query.edit_message_text(f"Order {order_id} marked as paid (SDK call).")
            else:
                await query.edit_message_text("Failed to call SDK mark paid method; check logs.")
        except Exception as e:
            logging.exception("Error marking paid: %s", e)
            await query.edit_message_text("Exception occurred while marking paid. See logs.")
        return

    if data.startswith("viewad:"):
        ad_id = data.split(":", 1)[1]
        for item in state.get("history", []):
            if item.get("ad", {}).get("id") == ad_id:
                await query.edit_message_text(f"Ad details:\n{json.dumps(item.get('ad',{}), indent=2)}")
                return
        await query.edit_message_text("Ad not found in recent history.")
        return

    if data == "back_main":
        kb = [
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("▶️ Start Bot", callback_data="start_bot"),
             InlineKeyboardButton("⏹ Stop Bot", callback_data="stop_bot")],
            [InlineKeyboardButton("📜 History", callback_data="history")],
            [InlineKeyboardButton("ℹ️ Status", callback_data="status")]
        ]
        await query.edit_message_text("Main Menu", reply_markup=InlineKeyboardMarkup(kb))
        return

    await query.edit_message_text("Unknown action.")

# messages for numeric inputs (min/max and range)
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    state = context.bot_data["state"]
    # try parse range like "1400-1455"
    if "-" in text and all(part.strip().replace(".", "").isdigit() for part in text.split("-", 1)):
        try:
            pmin, pmax = text.split("-", 1)
            pmin = float(pmin.strip())
            pmax = float(pmax.strip())
            if pmin <= pmax:
                state["price_range"]["min"] = pmin
                state["price_range"]["max"] = pmax
                save_state(state)
                await update.message.reply_text(f"Price range set to {pmin} - {pmax}")
                return
            else:
                await update.message.reply_text("Min must be <= Max. Try again.")
                return
        except Exception as e:
            logging.exception("Parse range error: %s", e)
            await update.message.reply_text("Failed to parse range. Use format: 1400-1455")
            return
    # try parse min_buy / max_buy single numbers
    if text.isdigit():
        n = int(text)
        if n <= state.get("max_buy", DEFAULT_STATE["max_buy"]):
            state["min_buy"] = n
            save_state(state)
            await update.message.reply_text(f"Min buy set to {n}")
            return
        else:
            state["max_buy"] = n
            save_state(state)
            await update.message.reply_text(f"Max buy set to {n}")
            return

    await update.message.reply_text("I didn't understand that. Send range like: 1400-1455 or a single number for min/max buy.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n/start - menu\nSend price range like '1400-1455' to set range.\nSend a number to set min or max buy (bot guesses).\nUse inline menu to fine-tune."
    )

def create_app_and_engine():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Please set TELEGRAM_TOKEN environment variable")
    state = load_state()
    application = ApplicationBuilder().token(token).build()
    engine = BotEngine(application, state)
    application.bot_data["state"] = state
    application.bot_data["engine"] = engine

    # handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_router))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    return application, engine, state

def main():
    app, engine, state = create_app_and_engine()
    # if auto_start true -> start crawler
    if state.get("auto_start") or state.get("bot_running"):
        engine.start()

    # start telegram polling
    logging.info("Starting Telegram bot polling")
    app.run_polling(stop_signals=None)

if __name__ == "__main__":
    main()
