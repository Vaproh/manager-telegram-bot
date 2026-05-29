from __future__ import annotations

import html
import io
import logging

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ALLOWED_USER_ID, BOT_NAME, BOT_TOKEN, SERVICE_NAME
from database import (
    add_account,
    add_accounts_bulk,
    add_category,
    add_retrieval_item,
    create_retrieval_session,
    delete_account,
    delete_category,
    export_accounts_csv,
    get_category_name,
    get_item,
    get_accounts_for_category,
    get_session,
    get_session_items,
    init_db,
    list_categories,
    list_pending_items,
    list_recent_sessions,
    normalize_name,
    search_accounts,
    set_item_used,
    stats_summary,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(BOT_NAME)

init_db()

pending_adds: dict[int, dict] = {}
pending_bulk: dict[int, dict] = {}
pending_gets: dict[int, dict] = {}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ALLOWED_USER_ID)


def allowed_guard(update: Update) -> bool:
    if is_allowed(update):
        return True
    user = update.effective_user
    if user:
        logger.warning("Unauthorized access attempt from user_id=%s", user.id)
    return False


def category_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows = list_categories()
    buttons = [[InlineKeyboardButton(row["name"], callback_data=f"{prefix}:{row['id']}")] for row in rows]
    return InlineKeyboardMarkup(buttons)


def fmt_account_block(index: int, username: str, password: str, category: str | None = None) -> str:
    category_line = f"\n│ <b>Category:</b> <code>{esc(category)}</code>" if category else ""
    return (
        f"╭─ <b>Account {index}</b> ─────────────────\n"
        f"│ <b>Username:</b> <code>{esc(username)}</code>\n"
        f"│ <b>Password:</b> <code>{esc(password)}</code>"
        f"{category_line}\n"
        f"╰──────────────────────────"
    )


async def set_commands(app: Application) -> None:
    commands = [
        BotCommand("start", "Show bot menu"),
        BotCommand("add", "Add one account"),
        BotCommand("bulkadd", "Add many accounts"),
        BotCommand("getaccounts", "Retrieve accounts by category"),
        BotCommand("search", "Search accounts"),
        BotCommand("delete", "Delete an account"),
        BotCommand("categories", "List categories"),
        BotCommand("addcategory", "Create a category"),
        BotCommand("deletecategory", "Delete a category"),
        BotCommand("logs", "View retrieval logs"),
        BotCommand("unused", "View unmarked retrievals"),
        BotCommand("markused", "Mark a retrieved account as used"),
        BotCommand("markunused", "Mark a retrieved account as unused"),
        BotCommand("stats", "Show statistics"),
        BotCommand("export", "Export accounts as CSV"),
    ]
    await app.bot.set_my_commands(commands)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    text = (
        f"<b>{esc(BOT_NAME)}</b>\n"
        f"<i>Private vault for {esc(SERVICE_NAME)} credentials</i>\n\n"
        f"<b>Commands</b>\n"
        f"• /add - add one account\n"
        f"• /bulkadd - add many accounts\n"
        f"• /getaccounts - retrieve accounts\n"
        f"• /search - search accounts\n"
        f"• /delete - delete by ID\n"
        f"• /categories - list categories\n"
        f"• /addcategory - create category\n"
        f"• /deletecategory - delete category\n"
        f"• /logs - recent retrieval logs\n"
        f"• /unused - pending retrieval items\n"
        f"• /markused - mark item used\n"
        f"• /markunused - mark item unused\n"
        f"• /stats - statistics\n"
        f"• /export - export CSV"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n<code>/add username password</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    username = context.args[0]
    password = " ".join(context.args[1:])

    pending_adds[update.effective_user.id] = {"username": username, "password": password}
    await update.effective_message.reply_text(
        "<b>Select a category</b>",
        reply_markup=category_keyboard("addcat"),
        parse_mode=ParseMode.HTML,
    )
    logger.info("User %s started add flow for username=%s", update.effective_user.id, username)


async def bulkadd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    pending_bulk[update.effective_user.id] = {"stage": "category"}
    await update.effective_message.reply_text(
        "<b>Select a category for the bulk import</b>",
        reply_markup=category_keyboard("bulkcat"),
        parse_mode=ParseMode.HTML,
    )
    logger.info("User %s started bulk add flow", update.effective_user.id)


async def getaccounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    pending_gets[update.effective_user.id] = {"stage": "category"}
    await update.effective_message.reply_text(
        "<b>Select a category</b>",
        reply_markup=category_keyboard("getcat"),
        parse_mode=ParseMode.HTML,
    )
    logger.info("User %s started getaccounts flow", update.effective_user.id)


async def addcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n<code>/addcategory Category Name</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name = normalize_name(" ".join(context.args))
    ok, message = add_category(name)
    if ok:
        logger.info("Category created: %s by user %s", name, update.effective_user.id)
        await update.effective_message.reply_text(
            f"<b>Category created</b>\n<code>{esc(name)}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_message.reply_text(
            f"<b>Could not create category</b>\n<code>{esc(message)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def deletecategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n<code>/deletecategory Category Name</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name = normalize_name(" ".join(context.args))
    ok, message = delete_category(name)
    if ok:
        logger.info("Category deleted: %s by user %s", name, update.effective_user.id)
        await update.effective_message.reply_text(
            f"<b>Category deleted</b>\n<code>{esc(name)}</code>\n<blockquote>Accounts moved to uncategorized.</blockquote>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_message.reply_text(
            f"<b>Could not delete category</b>\n<code>{esc(message)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    rows = list_categories()
    if not rows:
        await update.effective_message.reply_text("<b>No categories found</b>", parse_mode=ParseMode.HTML)
        return

    lines = ["<b>Categories</b>", ""]
    for row in rows:
        lines.append(f"• <code>{esc(row['name'])}</code>  <i>({row['account_count']})</i>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n<code>/search term</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    term = " ".join(context.args)
    rows = search_accounts(term)

    if not rows:
        await update.effective_message.reply_text(
            f"<b>No matches for</b> <code>{esc(term)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    parts = [f"<b>Search results for</b> <code>{esc(term)}</code>", ""]
    for row in rows[:20]:
        parts.append(
            "╭──────────────────────────\n"
            f"│ <b>ID:</b> <code>{row['id']}</code>\n"
            f"│ <b>Username:</b> <code>{esc(row['username'])}</code>\n"
            f"│ <b>Password:</b> <code>{esc(row['password'])}</code>\n"
            f"│ <b>Category:</b> <code>{esc(row['category'])}</code>\n"
            "╰──────────────────────────"
        )
    await update.effective_message.reply_text("\n\n".join(parts), parse_mode=ParseMode.HTML)


async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n<code>/delete account_id</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        account_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("<b>Account ID must be a number</b>", parse_mode=ParseMode.HTML)
        return

    ok = delete_account(account_id)
    if ok:
        logger.info("Account deleted: %s by user %s", account_id, update.effective_user.id)
        await update.effective_message.reply_text(
            f"<b>Deleted account</b>\n<code>{account_id}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_message.reply_text(
            f"<b>No account found with ID</b> <code>{account_id}</code>",
            parse_mode=ParseMode.HTML,
        )


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    rows = list_recent_sessions(10)
    if not rows:
        await update.effective_message.reply_text("<b>No retrieval sessions yet</b>", parse_mode=ParseMode.HTML)
        return

    text = ["<b>Recent retrieval sessions</b>", ""]
    keyboard = []
    for row in rows:
        text.append(
            "╭──────────────────────────\n"
            f"│ <b>Session:</b> <code>{row['id']}</code>\n"
            f"│ <b>Category:</b> <code>{esc(row['category'])}</code>\n"
            f"│ <b>Requested:</b> <code>{row['requested_amount']}</code>\n"
            f"│ <b>Retrieved:</b> <code>{row['retrieved_amount']}</code>\n"
            f"│ <b>Created:</b> <code>{esc(row['created_at'])}</code>\n"
            "╰──────────────────────────"
        )
        keyboard.append([InlineKeyboardButton(f"Session {row['id']}", callback_data=f"sess:{row['id']}")])

    await update.effective_message.reply_text(
        "\n\n".join(text),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def unused(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    rows = list_pending_items(25)
    if not rows:
        await update.effective_message.reply_text("<b>No pending retrieval items</b>", parse_mode=ParseMode.HTML)
        return

    text = ["<b>Pending retrieval items</b>", ""]
    keyboard = []
    for row in rows:
        text.append(
            "╭──────────────────────────\n"
            f"│ <b>Item:</b> <code>{row['item_id']}</code>\n"
            f"│ <b>Session:</b> <code>{row['session_id']}</code>\n"
            f"│ <b>Username:</b> <code>{esc(row['username'])}</code>\n"
            f"│ <b>Category:</b> <code>{esc(row['category'])}</code>\n"
            f"│ <b>Used:</b> <code>no</code>\n"
            "╰──────────────────────────"
        )
        keyboard.append([
            InlineKeyboardButton(f"Used {row['item_id']}", callback_data=f"itemused:{row['item_id']}"),
            InlineKeyboardButton(f"Unused {row['item_id']}", callback_data=f"itemunused:{row['item_id']}"),
        ])

    await update.effective_message.reply_text(
        "\n\n".join(text),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def markused(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n<code>/markused item_id</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("<b>Item ID must be a number</b>", parse_mode=ParseMode.HTML)
        return

    ok = set_item_used(item_id, True)
    if ok:
        logger.info("Marked item used: %s by user %s", item_id, update.effective_user.id)
        await update.effective_message.reply_text(
            f"<b>Marked as used</b>\n<code>{item_id}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_message.reply_text(
            f"<b>No item found with ID</b> <code>{item_id}</code>",
            parse_mode=ParseMode.HTML,
        )


async def markunused(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n<code>/markunused item_id</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("<b>Item ID must be a number</b>", parse_mode=ParseMode.HTML)
        return

    ok = set_item_used(item_id, False)
    if ok:
        logger.info("Marked item unused: %s by user %s", item_id, update.effective_user.id)
        await update.effective_message.reply_text(
            f"<b>Marked as unused</b>\n<code>{item_id}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_message.reply_text(
            f"<b>No item found with ID</b> <code>{item_id}</code>",
            parse_mode=ParseMode.HTML,
        )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    data = stats_summary()
    lines = [
        f"<b>{esc(BOT_NAME)} statistics</b>",
        "",
        f"• <b>Total accounts:</b> <code>{data['total_accounts']}</code>",
        f"• <b>Total retrieval sessions:</b> <code>{data['total_sessions']}</code>",
        f"• <b>Total retrieval items:</b> <code>{data['total_items']}</code>",
        f"• <b>Marked used:</b> <code>{data['used_items']}</code>",
        f"• <b>Pending:</b> <code>{data['pending_items']}</code>",
        "",
        "<b>By category</b>",
    ]
    for row in data["categories"]:
        lines.append(f"• <code>{esc(row['name'])}</code>  <i>({row['account_count']})</i>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    content = export_accounts_csv()
    if not content or len(content) <= 1:
        await update.effective_message.reply_text("<b>No accounts to export</b>", parse_mode=ParseMode.HTML)
        return

    from telegram import InputFile

    bio = io.BytesIO(content)
    bio.name = f"{SERVICE_NAME.lower().replace(' ', '_')}_accounts.csv"
    bio.seek(0)

    await update.effective_message.reply_document(
        document=InputFile(bio, filename=bio.name),
        caption=f"<b>{esc(BOT_NAME)} export</b>",
        parse_mode=ParseMode.HTML,
    )
    logger.info("Exported accounts for user %s", update.effective_user.id)


def parse_bulk_lines(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    items: list[tuple[str, str]] = []
    errors: list[str] = []

    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue

        delim = None
        for candidate in [",", ":", "|", "\t", ";"]:
            if candidate in line:
                delim = candidate
                break

        if delim:
            parts = [p.strip() for p in line.split(delim)]
        else:
            parts = line.split()

        if len(parts) < 2:
            errors.append(f"Line {i}: could not parse")
            continue

        username = parts[0]
        password = " ".join(parts[1:]).strip()

        if not username or not password:
            errors.append(f"Line {i}: empty value")
            continue

        items.append((username, password))

    return items, errors


async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    query = update.callback_query
    if not query:
        return

    await query.answer()

    try:
        prefix, cat_id_str = query.data.split(":", 1)
        category_id = int(cat_id_str)
    except Exception:
        await query.edit_message_text("Invalid selection.")
        return

    category_name = get_category_name(category_id)
    if not category_name:
        await query.edit_message_text("Category not found.")
        return

    user_id = query.from_user.id

    if prefix == "addcat":
        data = pending_adds.pop(user_id, None)
        if not data:
            await query.edit_message_text("Session expired.")
            return

        ok, message, account_id = add_account(data["username"], data["password"], category_id)
        if ok:
            logger.info(
                "Added account id=%s username=%s category=%s by user %s",
                account_id,
                data["username"],
                category_name,
                user_id,
            )
            await query.edit_message_text(
                f"<b>Account saved</b>\n"
                f"• <b>Username:</b> <code>{esc(data['username'])}</code>\n"
                f"• <b>Category:</b> <code>{esc(category_name)}</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(
                f"<b>Could not save account</b>\n<code>{esc(message)}</code>",
                parse_mode=ParseMode.HTML,
            )
        return

    if prefix == "bulkcat":
        pending_bulk[user_id] = {"category_id": category_id, "category_name": category_name, "stage": "lines"}
        await query.edit_message_text(
            f"<b>Bulk import category selected</b>\n"
            f"• <b>Category:</b> <code>{esc(category_name)}</code>\n\n"
            f"Send the lines now. Use one of these formats per line:\n"
            f"<code>username,password</code>\n"
            f"<code>username:password</code>\n"
            f"<code>username|password</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if prefix == "getcat":
        pending_gets[user_id] = {"category_id": category_id, "category_name": category_name, "stage": "count"}
        await query.edit_message_text(
            f"<b>Category selected</b>\n"
            f"• <b>Category:</b> <code>{esc(category_name)}</code>\n\n"
            f"Now send how many accounts you want.",
            parse_mode=ParseMode.HTML,
        )
        return


async def handle_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    query = update.callback_query
    if not query:
        return

    await query.answer()

    if query.data.startswith("sess:"):
        try:
            session_id = int(query.data.split(":", 1)[1])
        except ValueError:
            await query.edit_message_text("Invalid session.")
            return

        session = get_session(session_id)
        if not session:
            await query.edit_message_text("Session not found.")
            return

        items = get_session_items(session_id)
        if not items:
            await query.edit_message_text("No items found for this session.")
            return

        parts = [
            f"<b>Session {session['id']}</b>",
            f"• <b>Category:</b> <code>{esc(session['category'])}</code>",
            f"• <b>Requested:</b> <code>{session['requested_amount']}</code>",
            f"• <b>Retrieved:</b> <code>{session['retrieved_amount']}</code>",
            f"• <b>Created:</b> <code>{esc(session['created_at'])}</code>",
            "",
        ]
        keyboard = []
        for row in items:
            parts.append(fmt_account_block(row["position"], row["username"], row["password"], row["category"]))
            keyboard.append([
                InlineKeyboardButton(f"Used {row['item_id']}", callback_data=f"itemused:{row['item_id']}"),
                InlineKeyboardButton(f"Unused {row['item_id']}", callback_data=f"itemunused:{row['item_id']}"),
            ])

        await query.edit_message_text(
            "\n\n".join(parts),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
        )
        return

    if query.data.startswith("itemused:") or query.data.startswith("itemunused:"):
        try:
            item_id = int(query.data.split(":", 1)[1])
        except ValueError:
            await query.answer("Invalid item.")
            return

        mark_used = query.data.startswith("itemused:")
        ok = set_item_used(item_id, mark_used)
        if not ok:
            await query.answer("Item not found.")
            return

        logger.info(
            "Item %s marked %s by user %s",
            item_id,
            "used" if mark_used else "unused",
            query.from_user.id,
        )

        item = get_item(item_id)
        status = "used" if mark_used else "unused"
        if item:
            await query.edit_message_text(
                f"<b>Item updated</b>\n"
                f"• <b>Item:</b> <code>{item_id}</code>\n"
                f"• <b>Status:</b> <code>{status}</code>\n"
                f"• <b>Username:</b> <code>{esc(item['username'])}</code>\n"
                f"• <b>Category:</b> <code>{esc(item['category'])}</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(
                f"<b>Item updated</b>\n<code>{item_id}</code>",
                parse_mode=ParseMode.HTML,
            )
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    user_id = update.effective_user.id
    text = update.effective_message.text or ""

    if user_id in pending_bulk:
        data = pending_bulk.pop(user_id)
        items, errors = parse_bulk_lines(text)
        summary = add_accounts_bulk(items, data["category_id"])

        lines = [
            "<b>Bulk import complete</b>",
            f"• <b>Category:</b> <code>{esc(data['category_name'])}</code>",
            f"• <b>Added:</b> <code>{summary['added']}</code>",
            f"• <b>Skipped duplicates:</b> <code>{summary['skipped']}</code>",
            f"• <b>Failed:</b> <code>{summary['failed'] + len(errors)}</code>",
        ]
        if errors:
            lines.append("")
            lines.append("<b>Parsing issues</b>")
            for err in errors[:10]:
                lines.append(f"• <code>{esc(err)}</code>")

        logger.info(
            "Bulk import by user %s into category %s: added=%s skipped=%s failed=%s parse_errors=%s",
            user_id,
            data["category_name"],
            summary["added"],
            summary["skipped"],
            summary["failed"],
            len(errors),
        )

        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    if user_id in pending_gets:
        data = pending_gets.pop(user_id)
        try:
            amount = int(text.strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text(
                "<b>Send a valid number greater than zero</b>",
                parse_mode=ParseMode.HTML,
            )
            pending_gets[user_id] = data
            return

        available_rows = get_accounts_for_category(data["category_id"], amount)
        retrieved_amount = len(available_rows)

        session_id = create_retrieval_session(
            user_id=user_id,
            category_id=data["category_id"],
            requested_amount=amount,
            retrieved_amount=retrieved_amount,
        )

        for position, row in enumerate(available_rows, start=1):
            add_retrieval_item(session_id, row["id"], position)

        parts = [
            "<b>Retrieved accounts</b>",
            f"• <b>Session:</b> <code>{session_id}</code>",
            f"• <b>Category:</b> <code>{esc(data['category_name'])}</code>",
            f"• <b>Requested:</b> <code>{amount}</code>",
            f"• <b>Returned:</b> <code>{retrieved_amount}</code>",
            "",
        ]

        if not available_rows:
            parts.append("<b>No accounts available in this category</b>")
            logger.info(
                "Get accounts by user %s for category %s requested=%s returned=0",
                user_id,
                data["category_name"],
                amount,
            )
            await update.effective_message.reply_text("\n".join(parts), parse_mode=ParseMode.HTML)
            return

        for idx, row in enumerate(available_rows, start=1):
            parts.append(fmt_account_block(idx, row["username"], row["password"], row["category"]))

        logger.info(
            "Get accounts by user %s for category %s requested=%s returned=%s session=%s",
            user_id,
            data["category_name"],
            amount,
            retrieved_amount,
            session_id,
        )

        await update.effective_message.reply_text("\n\n".join(parts), parse_mode=ParseMode.HTML)
        return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)


async def post_init(app: Application) -> None:
    await set_commands(app)


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("bulkadd", bulkadd))
    app.add_handler(CommandHandler("getaccounts", getaccounts))
    app.add_handler(CommandHandler("addcategory", addcategory))
    app.add_handler(CommandHandler("deletecategory", deletecategory))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("unused", unused))
    app.add_handler(CommandHandler("markused", markused))
    app.add_handler(CommandHandler("markunused", markunused))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))

    app.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r"^(addcat|bulkcat|getcat):"))
    app.add_handler(CallbackQueryHandler(handle_session_callback, pattern=r"^(sess|itemused|itemunused):"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    app = build_app()
    logger.info("Starting bot: %s for service %s", BOT_NAME, SERVICE_NAME)
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
