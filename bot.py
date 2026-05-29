from __future__ import annotations

import csv
import html
import io
import logging
from pathlib import Path

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
    delete_accounts_by_ids,
    delete_accounts_in_category,
    delete_category,
    export_accounts_csv,
    get_category_name,
    get_item,
    get_category_id_by_name,
    get_accounts_for_category,
    get_unused_accounts_for_category,
    get_session,
    set_account_used,
    get_session_items,
    count_pending_items,
    list_accounts,
    count_accounts,
    get_account_by_id,
    init_db,
    list_categories,
    list_pending_items,
    list_recent_sessions,
    normalize_name,
    search_accounts,
    set_item_used,
    stats_summary,
)

ROOT_DIR = Path(__file__).resolve().parent
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log_file = LOG_DIR / f"{BOT_NAME.lower().replace(' ', '_')}.log"
log_formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

root_logger = logging.getLogger()
for _handler in list(root_logger.handlers):
    root_logger.removeHandler(_handler)
root_logger.setLevel(logging.WARNING)
for lib_name in ("telegram", "telegram.ext", "urllib3", "asyncio"):
    logging.getLogger(lib_name).setLevel(logging.WARNING)

logger = logging.getLogger(BOT_NAME)
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(file_handler)
logger.addHandler(console_handler)

init_db()

pending_adds: dict[int, dict] = {}
pending_bulk: dict[int, dict] = {}
pending_gets: dict[int, dict] = {}
pending_bulk_delete: dict[int, str] = {}
pending_bulk_delete_confirm: dict[int, str] = {}
pending_delete_confirm: dict[int, int] = {}
pending_delete_category_confirm: dict[int, str] = {}
pending_csv_extract: dict[int, bool] = {}


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


def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("➕ Add account", callback_data="menu:add"),
            InlineKeyboardButton("📂 Get accounts", callback_data="menu:get"),
        ],
        [
            InlineKeyboardButton("👥 Accounts", callback_data="menu:accounts"),
            InlineKeyboardButton("📋 List", callback_data="menu:list"),
        ],
        [
            InlineKeyboardButton("🔎 Search", callback_data="menu:search"),
            InlineKeyboardButton("📊 Stats", callback_data="menu:stats"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def fmt_account_block(index: int, username: str, password: str, category: str | None = None) -> str:
    category_line = f"\n│ <b>Category:</b> <code>{esc(category)}</code>" if category else ""
    return (
        f"╭─ <b>Account {index}</b> ─────────────────\n"
        f"│ <b>Username:</b> <code>{esc(username)}</code>\n"
        f"│ <b>Password:</b> <tg-spoiler>{esc(password)}</tg-spoiler>\n"
        f"│{category_line}\n"
        f"╰──────────────────────────"
    )


PENDING_PAGE_SIZE = 5
ACCOUNTS_PAGE_SIZE = 5
LIST_PAGE_SIZE = 5


def build_pending_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = count_pending_items()
    offset = page * PENDING_PAGE_SIZE
    rows = list_pending_items(PENDING_PAGE_SIZE, offset)

    if not rows:
        return (
            "<b>⏳ Pending retrieval items</b>\n\n<em>There are no pending items.</em>",
            InlineKeyboardMarkup([]),
        )

    text = [
        f"<b>⏳ Pending retrieval items</b>  <code>page {page + 1}/{max(1, (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE)}</code>"
    ]
    keyboard = []

    for row in rows:
        text.append(
            "╭──────────────────────────\n"
            f"│ <b>Item:</b> <code>{row['item_id']}</code>\n"
            f"│ <b>Session:</b> <code>{row['session_id']}</code>\n"
            f"│ <b>Username:</b> <code>{esc(row['username'])}</code>\n"
            f"│ <b>Category:</b> <code>{esc(row['category'])}</code>\n"
            f"│ <b>Status:</b> <code>pending</code>\n"
            "╰──────────────────────────"
        )
        keyboard.append([
            InlineKeyboardButton(
                "✅ Mark used",
                callback_data=f"itemused:{row['item_id']}:{page}",
            )
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"pending:page:{page - 1}"))
    if offset + len(rows) < total:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"pending:page:{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    return "\n\n".join(text), InlineKeyboardMarkup(keyboard)


def build_accounts_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = count_accounts()
    offset = page * ACCOUNTS_PAGE_SIZE
    rows = list_accounts(ACCOUNTS_PAGE_SIZE, offset)

    if not rows:
        return (
            "<b>👥 Accounts</b>\n\n<em>No accounts found.</em>",
            InlineKeyboardMarkup([]),
        )

    text = [
        f"<b>👥 Accounts</b>  <code>page {page + 1}/{max(1, (total + ACCOUNTS_PAGE_SIZE - 1) // ACCOUNTS_PAGE_SIZE)}</code>"
    ]
    keyboard = []

    for row in rows:
        status = "used" if row["used"] else "unused"
        text.append(
            "╭──────────────────────────\n"
            f"│ <b>ID:</b> <code>{row['id']}</code>\n"
            f"│ <b>Username:</b> <code>{esc(row['username'])}</code>\n"
            f"│ <b>Category:</b> <code>{esc(row['category'])}</code>\n"
            f"│ <b>Status:</b> <code>{status}</code>\n"
            f"│ <b>Saved:</b> <code>{esc(row['created_at'])}</code>\n"
            "╰──────────────────────────"
        )
        action_label = "♻️ Mark unused" if row["used"] else "🚫 Mark used"
        keyboard.append([
            InlineKeyboardButton(
                action_label,
                callback_data=f"accounttoggle:{row['id']}:{page}",
            )
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"accountpage:{page - 1}"))
    if offset + len(rows) < total:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"accountpage:{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    return "\n\n".join(text), InlineKeyboardMarkup(keyboard)


def build_list_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = count_accounts()
    offset = page * LIST_PAGE_SIZE
    rows = list_accounts(LIST_PAGE_SIZE, offset)

    if not rows:
        return (
            "<b>📋 Accounts</b>\n\n<em>No accounts found.</em>",
            InlineKeyboardMarkup([]),
        )

    text = [
        f"<b>📋 Accounts</b>  <code>page {page + 1}/{max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)}</code>"
    ]
    keyboard = []

    for row in rows:
        text.append(
            "• <b>ID:</b> <code>{id}</code>  |  "
            "<b>User:</b> <code>{username}</code>  |  "
            "<b>Category:</b> <code>{category}</code>".format(
                id=row['id'],
                username=esc(row['username']),
                category=esc(row['category']),
            )
        )

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"listpage:{page - 1}"))
    if offset + len(rows) < total:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"listpage:{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    return "\n".join(text), InlineKeyboardMarkup(keyboard)


async def set_commands(app: Application) -> None:
    commands = [
        BotCommand("start", "🤖 Show bot menu"),
        BotCommand("help", "❓ Show bot help"),
        BotCommand("add", "➕ Add one account"),
        BotCommand("bulkadd", "📥 Add many accounts"),
        BotCommand("getaccounts", "📂 Retrieve unused accounts"),
        BotCommand("search", "🔎 Search accounts"),
        BotCommand("delete", "🗑️ Delete an account"),
        BotCommand("categories", "🗂️ List categories"),
        BotCommand("addcategory", "🆕 Create a category"),
        BotCommand("deletecategory", "❌ Delete a category"),
        BotCommand("logs", "📜 View retrieval logs"),
        BotCommand("unused", "⏳ Pending retrieval items menu"),
        BotCommand("accounts", "👥 Manage account used status"),
        BotCommand("list", "📋 Browse accounts by page"),
        BotCommand("bulkdelete", "🗑️ Bulk delete accounts"),
        BotCommand("extractcsv", "📄 Extract User/Email + Password from CSV"),
        BotCommand("stats", "📊 Show statistics"),
        BotCommand("export", "💾 Export accounts as CSV"),
    ]
    await app.bot.set_my_commands(commands)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    await start(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    text = (
        f"<b>🤖 {esc(BOT_NAME)}</b>\n"
        f"<i>🔐 Private vault for {esc(SERVICE_NAME)} credentials</i>\n\n"
        f"<b>✨ Quick menu</b>\n"
        f"• /add — ➕ save one account\n"
        f"• /bulkadd — 📥 import multiple accounts\n"
        f"• /getaccounts — 📂 pull unused accounts\n"
        f"• /search — 🔎 find an account quickly\n"
        f"• /delete — 🗑️ remove an account by ID\n"
        f"• /categories — 🗂️ view all categories\n"
        f"• /addcategory — 🆕 create a category\n"
        f"• /deletecategory — ❌ remove a category\n"
        f"• /logs — 📜 recent retrieval activity\n"
        f"• /unused — ⏳ review pending retrieval items\n"
        f"• /accounts — 👥 manage used/unused status\n"
        f"• /list — 📋 browse accounts page by page\n"
        f"• /bulkdelete — 🗑️ delete by IDs or category\n"
        f"• /extractcsv — 📄 extract email/user + password from CSV\n"
        f"• /stats — 📊 see bot usage stats\n"
        f"• /export — 💾 export the full account list"
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n<code>➕ /add username password</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    username = context.args[0]
    password = " ".join(context.args[1:])

    pending_adds[update.effective_user.id] = {"username": username, "password": password}
    await update.effective_message.reply_text(
        "<b>📁 Select a category</b>\n<em>Choose where to save this account</em>",
        reply_markup=category_keyboard("addcat"),
        parse_mode=ParseMode.HTML,
    )
    logger.info("User %s started add flow for username=%s", update.effective_user.id, username)


async def bulkadd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    pending_bulk[update.effective_user.id] = {"stage": "category"}
    await update.effective_message.reply_text(
        "<b>📁 Select a category for the bulk import</b>\n<em>Then send each account on a new line</em>",
        reply_markup=category_keyboard("bulkcat"),
        parse_mode=ParseMode.HTML,
    )
    logger.info("User %s started bulk add flow", update.effective_user.id)


async def getaccounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    pending_gets[update.effective_user.id] = {"stage": "category"}
    await update.effective_message.reply_text(
        "<b>📁 Select a category</b>\n<em>Then send how many unused accounts to retrieve</em>",
        reply_markup=category_keyboard("getcat"),
        parse_mode=ParseMode.HTML,
    )
    logger.info("User %s started getaccounts flow", update.effective_user.id)


async def addcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n<code>🆕 /addcategory Category Name</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name = normalize_name(" ".join(context.args))
    ok, message = add_category(name)
    if ok:
        logger.info("Category created: %s by user %s", name, update.effective_user.id)
        await update.effective_message.reply_text(
            f"<b>✅ Category created</b>\n<code>{esc(name)}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_message.reply_text(
            f"<b>❌ Could not create category</b>\n<code>{esc(message)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def deletecategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n<code>❌ /deletecategory Category Name</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name = normalize_name(" ".join(context.args))
    if get_category_id_by_name(name) is None:
        await update.effective_message.reply_text(
            f"<b>❌ Category not found</b>\n<code>{esc(name)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    pending_delete_category_confirm[update.effective_user.id] = name
    await update.effective_message.reply_text(
        f"<b>⚠️ Delete category</b> <code>{esc(name)}</code>?\n"
        "All accounts in this category will be moved to uncategorized.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"delcatconfirm:{esc(name)}"),
                InlineKeyboardButton("❌ Cancel", callback_data="delcatcancel:0"),
            ]
        ]),
        parse_mode=ParseMode.HTML,
    )


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    rows = list_categories()
    if not rows:
        await update.effective_message.reply_text("<b>📭 No categories found</b>", parse_mode=ParseMode.HTML)
        return

    lines = ["<b>📂 Categories</b>"]
    for row in rows:
        lines.append(f"• <code>{esc(row['name'])}</code>  <i>({row['account_count']})</i>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    raw = " ".join(context.args).strip()
    if not raw:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n"
            "<code>🔎 /search term</code>\n"
            "Examples:\n"
            "• /search gmail\n"
            "• /search category:finance used\n"
            "• /search old password oldest",
            parse_mode=ParseMode.HTML,
        )
        return

    tokens = raw.split()
    category = None
    used = None
    newest_first = True

    filtered_tokens = []
    for token in tokens:
        lower = token.lower()
        if lower.startswith("category:") or lower.startswith("cat:"):
            category = token.split(":", 1)[1].strip()
        elif lower in ("used", "unused"):
            used = lower == "used"
        elif lower in ("newest", "oldest"):
            newest_first = lower == "newest"
        else:
            filtered_tokens.append(token)

    term = " ".join(filtered_tokens).strip()
    rows = search_accounts(term, category=category, used=used, newest_first=newest_first)

    if not rows:
        await update.effective_message.reply_text(
            f"<b>🔍 No matches for</b> <code>{esc(term)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    filters = []
    if category:
        filters.append(f"Category: <code>{esc(category)}</code>")
    if used is not None:
        filters.append(f"Status: <code>{'used' if used else 'unused'}</code>")
    filters.append(f"Sort: <code>{'newest' if newest_first else 'oldest'}</code>")

    parts = [f"<b>Search results for</b> <code>{esc(term or 'all')}</code>"]
    if filters:
        parts.append("• " + " | ".join(filters))
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
            "<b>Usage</b>\n<code>🗑️ /delete account_id</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        account_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("<b>❗ Account ID must be a number</b>", parse_mode=ParseMode.HTML)
        return

    if not get_account_by_id(account_id):
        await update.effective_message.reply_text(
            f"<b>❌ No account found with ID</b> <code>{account_id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    pending_delete_confirm[update.effective_user.id] = account_id
    await update.effective_message.reply_text(
        f"<b>⚠️ Delete account #{account_id}?</b>\n"
        "This action cannot be undone.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm delete", callback_data=f"delconfirm:{account_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="delcancel:0"),
            ]
        ]),
        parse_mode=ParseMode.HTML,
    )


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    rows = list_recent_sessions(10)
    if not rows:
        await update.effective_message.reply_text("<b>📭 No retrieval sessions yet</b>", parse_mode=ParseMode.HTML)
        return

    text = ["<b>Recent retrieval sessions</b>"]
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

    text, reply_markup = build_pending_page(0)
    await update.effective_message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


async def accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    text, reply_markup = build_accounts_page(0)
    await update.effective_message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


async def list_accounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    text, reply_markup = build_list_page(0)
    await update.effective_message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


async def bulkdelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if context.args:
        raw = " ".join(context.args)
        pending_bulk_delete_confirm[update.effective_user.id] = raw
        await update.effective_message.reply_text(
            "<b>⚠️ Confirm bulk deletion</b>\n"
            "This will remove all matching accounts permanently.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Confirm delete", callback_data="bulkconfirm:1"),
                    InlineKeyboardButton("❌ Cancel", callback_data="bulkcancel:0"),
                ]
            ]),
            parse_mode=ParseMode.HTML,
        )
        return

    pending_bulk_delete[update.effective_user.id] = "active"
    await update.effective_message.reply_text(
        "<b>🗑️ Bulk delete</b>\n"
        "Send account IDs separated by spaces or commas, or a category name to delete all accounts in that category.",
        parse_mode=ParseMode.HTML,
    )


async def extractcsv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    pending_csv_extract[update.effective_user.id] = True
    await update.effective_message.reply_text(
        "<b>📄 Upload a CSV file</b>\nI will extract the <b>User ID / Email Address</b> and <b>Password</b> columns into a new CSV.",
        parse_mode=ParseMode.HTML,
    )


async def _bulk_delete_from_input(update: Update, raw: str) -> None:
    text = raw.strip()
    if not text:
        await update.effective_message.reply_text("<b>❗ Nothing to delete</b>", parse_mode=ParseMode.HTML)
        return

    ids = []
    for token in text.replace(',', ' ').split():
        try:
            ids.append(int(token))
        except ValueError:
            pass

    if ids:
        deleted = delete_accounts_by_ids(ids)
        await update.effective_message.reply_text(
            f"<b>🗑️ Deleted {deleted} account(s)</b>",
            parse_mode=ParseMode.HTML,
        )
        logger.info("Bulk deleted accounts by user %s: ids=%s deleted=%s", update.effective_user.id, ids, deleted)
        return

    category_name = normalize_name(text)
    category_id = get_category_id_by_name(category_name)
    if category_id is None:
        await update.effective_message.reply_text(
            f"<b>❌ Category not found</b> <code>{esc(category_name)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    deleted = delete_accounts_in_category(category_id)
    await update.effective_message.reply_text(
        f"<b>🗑️ Deleted {deleted} account(s)</b> from <code>{esc(category_name)}</code>",
        parse_mode=ParseMode.HTML,
    )
    logger.info("Bulk deleted accounts by user %s: category=%s deleted=%s", update.effective_user.id, category_name, deleted)


async def handle_csv_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    if update.effective_user.id not in pending_csv_extract:
        return

    document = update.effective_message.document
    if not document:
        return

    try:
        file = await document.get_file()
        raw = await file.download_as_bytearray()
    except Exception as exc:
        logger.warning("CSV upload failed for user %s: %s", update.effective_user.id, exc)
        await update.effective_message.reply_text("<b>❌ Could not read the uploaded CSV</b>", parse_mode=ParseMode.HTML)
        return

    try:
        text = bytes(raw).decode("utf-8-sig")
    except UnicodeDecodeError:
        text = bytes(raw).decode("cp1252", errors="ignore")

    try:
        from telegram import InputFile

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("No header row found")

        user_header = None
        password_header = None
        for header in reader.fieldnames:
            norm = (header or "").lower().replace(" ", "").replace("_", "").replace("-", "")
            if user_header is None and any(token in norm for token in ("userid", "user", "email", "username", "account")):
                user_header = header
            if password_header is None and "password" in norm:
                password_header = header

        if not user_header or not password_header:
            raise ValueError("Could not find the required columns")

        extracted_rows = []
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(["User ID / Email Address", "Password"])
        for row in reader:
            username = (row.get(user_header, "") or "").strip()
            password = (row.get(password_header, "") or "").strip()
            if username and password:
                extracted_rows.append((username, password))
            writer.writerow([username, password])

        category_id = get_category_id_by_name("uncategorized")
        if category_id is None:
            ok, _message = add_category("uncategorized")
            if not ok:
                raise RuntimeError("Could not create uncategorized category")
            category_id = get_category_id_by_name("uncategorized")

        summary = add_accounts_bulk(extracted_rows, category_id)

        bio = io.BytesIO(out.getvalue().encode("utf-8"))
        bio.name = "extracted_accounts.csv"
        bio.seek(0)
        await update.effective_message.reply_document(
            document=InputFile(bio, filename=bio.name),
            caption=(
                "<b>📄 Extracted CSV</b>\n"
                f"User/Email + Password only.\n"
                f"Stored: <code>{summary['added']}</code> new account(s), "
                f"skipped duplicates: <code>{summary['skipped']}</code>."
            ),
            parse_mode=ParseMode.HTML,
        )
        logger.info("CSV extracted for user %s: added=%s skipped=%s", update.effective_user.id, summary['added'], summary['skipped'])
    except Exception as exc:
        logger.warning("CSV extraction failed for user %s: %s", update.effective_user.id, exc)
        await update.effective_message.reply_text(
            "<b>❌ Could not extract the columns</b>\nMake sure the CSV has a User/Email column and a Password column.",
            parse_mode=ParseMode.HTML,
        )
    finally:
        pending_csv_extract.pop(update.effective_user.id, None)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    data = stats_summary()
    lines = [
        f"<b>📊 {esc(BOT_NAME)} statistics</b>",
        "",
        f"• <b>Total accounts:</b> <code>{data['total_accounts']}</code>",
        f"• <b>Total retrieval sessions:</b> <code>{data['total_sessions']}</code>",
        f"• <b>Total retrieval items:</b> <code>{data['total_items']}</code>",
        f"• <b>Marked used:</b> <code>{data['used_items']}</code>",
        f"• <b>Pending:</b> <code>{data['pending_items']}</code>",
        "",
        "<b>📂 By category</b>",
    ]
    for row in data["categories"]:
        lines.append(f"• <code>{esc(row['name'])}</code>  <i>({row['account_count']})</i>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    content = export_accounts_csv()
    if not content or len(content) <= 1:
        await update.effective_message.reply_text("<b>📭 No accounts to export</b>", parse_mode=ParseMode.HTML)
        return

    from telegram import InputFile

    bio = io.BytesIO(content)
    bio.name = f"{SERVICE_NAME.lower().replace(' ', '_')}_accounts.csv"
    bio.seek(0)

    await update.effective_message.reply_document(
        document=InputFile(bio, filename=bio.name),
        caption=f"<b>💾 {esc(BOT_NAME)} export</b>",
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
        await query.edit_message_text("❌ Invalid selection.")
        return

    category_name = get_category_name(category_id)
    if not category_name:
        await query.edit_message_text("❌ Category not found.")
        return

    user_id = query.from_user.id

    if prefix == "addcat":
        data = pending_adds.pop(user_id, None)
        if not data:
            await query.edit_message_text("⏳ Session expired.")
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
                f"<b>✅ Account saved</b>\n"
                f"• <b>Username:</b> <code>{esc(data['username'])}</code>\n"
                f"• <b>Category:</b> <code>{esc(category_name)}</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(
                f"<b>❌ Could not save account</b>\n<code>{esc(message)}</code>",
                parse_mode=ParseMode.HTML,
            )
        return

    if prefix == "bulkcat":
        pending_bulk[user_id] = {"category_id": category_id, "category_name": category_name, "stage": "lines"}
        await query.edit_message_text(
            f"<b>📥 Bulk import category selected</b>\n"
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
            f"<b>📁 Category selected</b>\n"
            f"• <b>Category:</b> <code>{esc(category_name)}</code>\n\n"
            f"Now send how many accounts you want.",
            parse_mode=ParseMode.HTML,
        )
        return


async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    action = data.split(":", 1)[1] if data.startswith("menu:") and ":" in data else data

    if data == "menu:add":
        await query.edit_message_text(
            "<b>➕ Choose how to add an account</b>\n"
            "Pick the method you want to use.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🧾 One account", callback_data="menu:add:single"),
                    InlineKeyboardButton("📥 Bulk import", callback_data="menu:add:bulk"),
                ],
                [InlineKeyboardButton("⬅ Back", callback_data="menu:back")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "menu:add:single":
        await query.edit_message_text(
            "<b>➕ Add one account</b>\n"
            "Send: <code>/add username password</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "menu:add:bulk":
        await query.edit_message_text(
            "<b>📥 Bulk add</b>\n"
            "Choose a category first, then send your accounts line by line.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "menu:back":
        await query.edit_message_text(
            "<b>✨ Quick menu</b>\n"
            "Tap one of the buttons below to jump to the main actions.",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    help_text = {
        "add": "Use /add <username> <password> to save one account.",
        "get": "Use /getaccounts to retrieve unused accounts from a category.",
        "accounts": "Use /accounts to review and toggle used/unused accounts.",
        "list": "Use /list to browse your account inventory page by page.",
        "search": "Use /search <term> or add filters like category:finance used newest.",
        "stats": "Use /stats to see account and retrieval summaries.",
    }.get(action, "Choose an action from the menu.")

    await query.edit_message_text(f"<b>✨ Quick action</b>\n{help_text}", parse_mode=ParseMode.HTML)


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
            await query.edit_message_text("❌ Invalid session.")
            return

        session = get_session(session_id)
        if not session:
            await query.edit_message_text("❌ Session not found.")
            return

        items = get_session_items(session_id)
        if not items:
            await query.edit_message_text("⚠️ No items found for this session.")
            return

        parts = [
            f"<b>📦 Session {session['id']}</b>",
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
                InlineKeyboardButton(f"✅ Mark used", callback_data=f"itemused:{row['item_id']}:0"),
                InlineKeyboardButton(f"♻️ Mark unused", callback_data=f"itemunused:{row['item_id']}:0"),
            ])

        await query.edit_message_text(
            "\n\n".join(parts),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
        )
        return

    if query.data.startswith("pending:page:"):
        try:
            page = int(query.data.split(":", 2)[2])
        except ValueError:
            await query.answer("Invalid page.")
            return

        text, reply_markup = build_pending_page(page)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return

    if query.data.startswith("itemused:") or query.data.startswith("itemunused:"):
        parts = query.data.split(":")
        if len(parts) < 2:
            await query.answer("Invalid item.")
            return

        try:
            item_id = int(parts[1])
        except ValueError:
            await query.answer("Invalid item.")
            return

        page = int(parts[2]) if len(parts) > 2 else 0
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

        text, reply_markup = build_pending_page(page)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return

    if query.data.startswith("accountpage:"):
        try:
            page = int(query.data.split(":", 2)[2])
        except ValueError:
            await query.answer("Invalid page.")
            return

        text, reply_markup = build_accounts_page(page)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return

    if query.data.startswith("delconfirm:"):
        try:
            account_id = int(query.data.split(":", 1)[1])
        except ValueError:
            await query.answer("Invalid account.")
            return

        ok = delete_account(account_id)
        pending_delete_confirm.pop(query.from_user.id, None)
        if ok:
            await query.edit_message_text(f"<b>🗑️ Deleted account</b> <code>{account_id}</code>", parse_mode=ParseMode.HTML)
        else:
            await query.edit_message_text(f"<b>❌ Could not delete account</b> <code>{account_id}</code>", parse_mode=ParseMode.HTML)
        return

    if query.data.startswith("delcancel:"):
        pending_delete_confirm.pop(query.from_user.id, None)
        await query.edit_message_text("<b>❌ Deletion cancelled</b>", parse_mode=ParseMode.HTML)
        return

    if query.data.startswith("delcatconfirm:"):
        name = query.data.split(":", 1)[1]
        name = html.unescape(name)
        pending_delete_category_confirm.pop(query.from_user.id, None)
        ok, message = delete_category(name)
        if ok:
            await query.edit_message_text(
                f"<b>🗑️ Category deleted</b>\n<code>{esc(name)}</code>\n<blockquote>Accounts moved to uncategorized.</blockquote>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(f"<b>❌ Could not delete category</b>\n<code>{esc(message)}</code>", parse_mode=ParseMode.HTML)
        return

    if query.data.startswith("delcatcancel:"):
        pending_delete_category_confirm.pop(query.from_user.id, None)
        await query.edit_message_text("<b>❌ Category deletion cancelled</b>", parse_mode=ParseMode.HTML)
        return

    if query.data.startswith("bulkconfirm:"):
        raw = pending_bulk_delete_confirm.pop(query.from_user.id, None)
        if not raw:
            await query.edit_message_text("<b>⏳ Confirmation expired</b>", parse_mode=ParseMode.HTML)
            return
        await query.edit_message_text("<b>🗑️ Deleting matching accounts…</b>", parse_mode=ParseMode.HTML)
        await _bulk_delete_from_input(update, raw)
        return

    if query.data.startswith("bulkcancel:"):
        pending_bulk_delete_confirm.pop(query.from_user.id, None)
        await query.edit_message_text("<b>❌ Bulk deletion cancelled</b>", parse_mode=ParseMode.HTML)
        return

    if query.data.startswith("listpage:"):
        try:
            page = int(query.data.split(":", 1)[1])
        except ValueError:
            await query.answer("Invalid page.")
            return

        text, reply_markup = build_list_page(page)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return

    if query.data.startswith("accounttoggle:"):
        parts = query.data.split(":")
        if len(parts) < 3:
            await query.answer("Invalid action.")
            return

        try:
            account_id = int(parts[1])
            page = int(parts[2])
        except ValueError:
            await query.answer("Invalid action.")
            return

        account = get_account_by_id(account_id)
        if not account:
            await query.answer("Account not found.")
            return

        mark_used = not bool(account["used"])
        ok = set_account_used(account_id, mark_used)
        if not ok:
            await query.answer("Unable to update account.")
            return

        logger.info(
            "Account %s marked %s by user %s",
            account_id,
            "used" if mark_used else "unused",
            query.from_user.id,
        )

        text, reply_markup = build_accounts_page(page)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_guard(update):
        return

    user_id = update.effective_user.id
    text = update.effective_message.text or ""

    if user_id in pending_bulk_delete:
        pending_bulk_delete.pop(user_id, None)
        await _bulk_delete_from_input(update, text)
        return

    if user_id in pending_bulk:
        data = pending_bulk.pop(user_id)
        items, errors = parse_bulk_lines(text)
        summary = add_accounts_bulk(items, data["category_id"])

        lines = [
            "<b>📥 Bulk import complete</b>",
            f"• <b>Category:</b> <code>{esc(data['category_name'])}</code>",
            f"• <b>Added:</b> <code>{summary['added']}</code>",
            f"• <b>Skipped duplicates:</b> <code>{summary['skipped']}</code>",
            f"• <b>Failed:</b> <code>{summary['failed'] + len(errors)}</code>",
        ]
        if errors:
            lines.append("")
            lines.append("<b>⚠️ Parsing issues</b>")
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
                "<b>❗ Send a valid number greater than zero</b>",
                parse_mode=ParseMode.HTML,
            )
            pending_gets[user_id] = data
            return

        available_rows = get_unused_accounts_for_category(data["category_id"], amount)
        retrieved_amount = len(available_rows)

        session_id = create_retrieval_session(
            user_id=user_id,
            category_id=data["category_id"],
            requested_amount=amount,
            retrieved_amount=retrieved_amount,
        )

        for position, row in enumerate(available_rows, start=1):
            add_retrieval_item(session_id, row["id"], position)

        heading_lines = [
            "<b>📦 Retrieved accounts</b>",
            f"• <b>Session:</b> <code>{session_id}</code>",
            f"• <b>Category:</b> <code>{esc(data['category_name'])}</code>",
            f"• <b>Requested:</b> <code>{amount}</code>",
            f"• <b>Returned:</b> <code>{retrieved_amount}</code>",
            "",
            "<i>Passwords are hidden as spoilers for safety — tap to reveal.</i>",
        ]

        if not available_rows:
            heading_lines.append("<b>⚠️ No unused accounts available in this category</b>")
            logger.info(
                "Get accounts by user %s for category %s requested=%s returned=0",
                user_id,
                data["category_name"],
                amount,
            )
            await update.effective_message.reply_text("\n".join(heading_lines), parse_mode=ParseMode.HTML)
            return

        body = [fmt_account_block(idx, row["username"], row["password"], row["category"]) for idx, row in enumerate(available_rows, start=1)]

        logger.info(
            "Get accounts by user %s for category %s requested=%s returned=%s session=%s",
            user_id,
            data["category_name"],
            amount,
            retrieved_amount,
            session_id,
        )

        await update.effective_message.reply_text(
            "\n\n".join(["\n".join(heading_lines), "\n\n".join(body)]),
            parse_mode=ParseMode.HTML,
        )
        return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)


async def post_init(app: Application) -> None:
    await set_commands(app)


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
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
    app.add_handler(CommandHandler("accounts", accounts))
    app.add_handler(CommandHandler("list", list_accounts_cmd))
    app.add_handler(CommandHandler("bulkdelete", bulkdelete))
    app.add_handler(CommandHandler("extractcsv", extractcsv))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))

    app.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r"^(addcat|bulkcat|getcat):"))
    app.add_handler(CallbackQueryHandler(handle_main_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(handle_session_callback, pattern=r"^(sess|pending|itemused|itemunused|accountpage|accounttoggle|listpage|delconfirm|delcancel|delcatconfirm|delcatcancel|bulkconfirm|bulkcancel):"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_csv_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    app = build_app()
    logger.info("Starting bot: %s for service %s", BOT_NAME, SERVICE_NAME)
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
