"""
Telegram bot: sends a formatted alert per candidate with inline
Approve/Reject buttons, and listens for the callback to drive execution.

Swap-out points if you'd rather use Discord or Pushover instead:
- Discord: use a webhook for one-way alerts, but you lose native inline
  buttons for approval without standing up your own small web server to
  receive interaction callbacks.
- Pushover: simplest one-way push, no native approve/reject buttons either.
Telegram is used here specifically because python-telegram-bot gives you
inline buttons + callback handling out of the box.
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

log = logging.getLogger("notifier")


def format_candidate_message(candidate: dict) -> str:
    if candidate.get("trigger_type"):
        lines = [
            f"*Exit: {candidate['trigger_type'].replace('_', ' ').title()}* — {candidate['symbol']}",
            f"Strategy: {candidate['strategy']}",
            f"Setup: {candidate['description']}",
            f"Contract(s): {candidate['legs_summary']}",
            f"Entry price: ${candidate.get('entry_price', 0):.2f}   Current MTM: ${candidate['est_price']:.2f}",
        ]
    else:
        lines = [
            f"*{candidate['strategy'].replace('_', ' ').title()}* — {candidate['symbol']}",
            f"Setup: {candidate['description']}",
            f"Contract(s): {candidate['legs_summary']}",
            f"Expiration: {candidate['expiration']}   DTE: {candidate['dte']}",
            f"Est. credit/debit: ${candidate['est_price']:.2f}",
        ]
        if candidate.get("max_loss_per_contract"):
            lines.append(f"Max loss/contract: ${candidate['max_loss_per_contract']:.2f}")
        if candidate.get("suggested_qty"):
            lines.append(f"Suggested qty: {candidate['suggested_qty']} "
                          f"(~${candidate.get('total_risk_at_suggested_qty', 0):.2f} total risk)")

        score_line = f"Score: {candidate['score']:.2f}"
        if candidate.get("learning_multiplier") is not None and candidate.get("score_before_learning") is not None:
            score_line += (f" (base {candidate['score_before_learning']:.2f} × "
                            f"{candidate['learning_multiplier']:.2f}x learned adjustment)")
        if candidate.get("ml_win_probability") is not None:
            score_line += f", ML win prob {candidate['ml_win_probability']:.0%}"
        lines.append(score_line)

        if candidate.get("macro_regime"):
            sent_line = f"Sentiment: {candidate['macro_regime']} regime ({candidate['regime_multiplier']:.2f}x)"
            if candidate.get("news_sentiment_multiplier") is not None and candidate["news_sentiment_multiplier"] != 1.0:
                sent_line += f", news dampening {candidate['news_sentiment_multiplier']:.2f}x"
            if candidate.get("vix_band_multiplier") is not None and candidate["vix_band_multiplier"] != 1.0:
                sent_line += f", VIX outside band {candidate['vix_band_multiplier']:.2f}x"
            lines.append(sent_line)

        if candidate.get("bybit_alpha_multiplier") is not None and candidate["bybit_alpha_multiplier"] != 1.0:
            alpha_line = (
                f"Bybit alpha: {candidate.get('bybit_alpha_bias', 'NEUTRAL')} "
                f"({candidate['bybit_alpha_multiplier']:.2f}x"
            )
            parts = []
            if candidate.get("bybit_alpha_ls_multiplier") not in (None, 1.0):
                parts.append(f"L/S {candidate['bybit_alpha_ls_multiplier']:.2f}x")
            if candidate.get("bybit_alpha_funding_multiplier") not in (None, 1.0):
                parts.append(f"funding {candidate['bybit_alpha_funding_multiplier']:.2f}x")
            if candidate.get("bybit_alpha_skew_multiplier") not in (None, 1.0):
                parts.append(f"skew {candidate['bybit_alpha_skew_multiplier']:.2f}x")
            if parts:
                alpha_line += ", " + ", ".join(parts)
            alpha_line += ")"
            if candidate.get("score_before_bybit_alpha") is not None:
                alpha_line = (
                    f"Score after alpha: {candidate['score']:.2f} "
                    f"(pre-alpha {candidate['score_before_bybit_alpha']:.2f}) — {alpha_line}"
                )
            lines.append(alpha_line)
        elif candidate.get("bybit_alpha_bias"):
            lines.append(f"Bybit alpha: {candidate['bybit_alpha_bias']} (no score adjustment)")

        if not candidate.get("trigger_type"):
            lines.append(f"Rationale: {candidate.get('rationale', '')}")
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, cfg: dict, db, executor):
        tg_cfg = cfg["telegram"]
        self.chat_id = tg_cfg["chat_id"]
        self.db = db
        self.executor = executor
        self.app = Application.builder().token(tg_cfg["bot_token"]).build()
        self.app.add_handler(CallbackQueryHandler(self._on_callback))

    async def send_approval_request(self, approval_id: str, candidate: dict):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{approval_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{approval_id}"),
        ]])
        text = format_candidate_message(candidate)
        await self.app.bot.send_message(
            chat_id=self.chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard
        )

    async def send_note(self, text: str, **kwargs):
        await self.app.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        action, approval_id = query.data.split(":", 1)

        record = self.db.get_approval(approval_id)
        if not record:
            await query.edit_message_text("This request no longer exists.")
            return
        if record["status"] != "PENDING":
            await query.edit_message_text(f"Already {record['status'].lower()}.")
            return

        if action == "reject":
            self.db.set_status(approval_id, "REJECTED")
            await query.edit_message_text(f"❌ Rejected: {record['symbol']} {record['strategy']}")
            return

        # action == approve
        self.db.set_status(approval_id, "APPROVED")
        await query.edit_message_text(f"✅ Approved: {record['symbol']} {record['strategy']}\nPlacing order...")
        try:
            order_id = await self.executor.execute(record["candidate"], approval_id)
            self.db.set_status(approval_id, "EXECUTED")
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=f"📈 Order placed for {record['symbol']} — Schwab order id: {order_id}",
            )
        except Exception as e:
            log.exception("Execution failed for %s", approval_id)
            self.db.set_status(approval_id, "FAILED")
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=f"⚠️ Order FAILED for {record['symbol']}: {e}",
            )

    async def send_learning_report(self, summary: str, newly_banned: list, active_bans: list):
        lines = ["*Weekly learning & guardrails report*", summary]
        if newly_banned:
            lines.append("\n*Newly banned symbols*")
            for symbol, reason in newly_banned:
                lines.append(f"• {symbol}: {reason}")
        if active_bans:
            lines.append("\n*Active bans*")
            for ban in active_bans:
                exp = ban.get("expires_at", "never")
                lines.append(f"• {ban['symbol']}: {ban['reason']} (expires {exp})")
        await self.send_note("\n".join(lines))

    async def run_polling_forever(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()