from .shared import *
from .events_core import *
from .state import *
from .runtime import *
from .ai_center import *
from . import ai_center as ai_center_module

async def register_and_notify_new_user(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database) -> bool:
    """
    Save frontend user in DB and notify admins on first interaction.
    Returns True if user was inserted (new), False otherwise.
    """
    user = update.effective_user
    if not user:
        return False

    uid = user.id
    uname = user.username
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        row = await db.fetchone("SELECT user_id FROM bot_users WHERE user_id=?", (uid,))
        is_new = row is None

        if is_new:
            await db.execute(
                "INSERT INTO bot_users (user_id, username, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                (uid, uname, now_iso, now_iso),
            )
        else:
            await db.execute(
                "UPDATE bot_users SET username=?, last_seen_at=? WHERE user_id=?",
                (uname, now_iso, uid),
            )

        if is_new:
            await send_admin_approval_request(
                db,
                context.bot,
                uid,
                uname
            )

            display_name = uname or f"ID {uid}"
            msg_text = (
                f"👤 <b>Новый пользователь бота</b>\n"
                f"ID: <code>{uid}</code>\n"
                f"Username: {html.escape(display_name, quote=False)}"
            )

            for admin_id in CONFIG.admin_ids:
                try:
                    await send_and_log(
                        context.bot,
                        admin_id,
                        msg_text,
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    logger.exception(
                        "Failed to notify admin %s about new user %s",
                        admin_id,
                        uid
                    )

        return is_new

    except Exception:
        logger.exception("register_and_notify_new_user error for %s", uid)
        return False
# --- Commands & helpers ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    user = update.effective_user
    uid  = user.id
    uname = user.username

    app = context.bot_data.get("app")
    if not app:
        if update.message:
            await update.message.reply_text("❌ Ошибка инициализации. Попробуйте позже.")
        return

    log_frontend_incoming(uid, uname, text="/start", meta="cmd=/start")
    is_new = await register_and_notify_new_user(update, context, app.db)

    # Блокируем доступ до одобрения (кроме админов)
    if uid not in CONFIG.admin_ids and not await is_user_fully_approved(context, uid):
        await update.message.reply_text(
            "⏳ Запрос на доступ отправлен администратору. Пока что вы не можете начать авторизацию."
        )
        return

    kb = build_start_keyboard()

    if is_new:
        welcome_text = (
"<b>Saved Delete Messages</b> 🔐\n\n"

"Это конфиденциальный бот для сохранения сообщений, которые были удалены, изменены или исчезли из-за таймера самоуничтожения.\n\n"

"Бот автоматически сохраняет:\n"
"• удалённые сообщения\n"
"• исходные версии отредактированных сообщений\n"
"• сообщения с таймером самоуничтожения\n\n"

"Все данные сохраняются только в вашем личном архиве.\n"
"Доступ к информации есть только у вас.\n\n"

"Благодаря архитектуре хранения и изоляции данных информация не передаётся третьим лицам и не используется вне вашего архива.\n\n"

"<b>Выберите вариант входа ниже.</b>"
        )

        m = await send_and_log(
            context.bot,
            uid,
            welcome_text,
            username=uname,
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

        try:
            if hasattr(app, "auth") and callable(getattr(app.auth, "track_auth_message", None)):
                app.auth.track_auth_message(uid, m.message_id)
        except Exception:
            logger.debug("Failed to track welcome message uid=%s", uid)

        return

    # ------------------------------------------------
    # SESSION CHECK
    # ------------------------------------------------

    session_valid = False

    if app.storage.is_valid(uid):

        try:
            app.watcher_service.ensure(uid)
        except Exception:
            logger.debug("watcher_service.ensure failed uid=%s", uid)

        session_valid = await app.storage.is_session_valid(uid)

    if session_valid:

        kb_stats = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Статистика", callback_data="stats")]
        ])

        text = (
            "✅ <b>Сессия активна</b>\n\n"
            "Бот уже работает в фоне и сохраняет:\n"
            "• удалённые сообщения\n"
            "• редактирования сообщений\n"
            "• исчезающие медиа\n\n"
            "Нажмите «Статистика», чтобы увидеть данные."
        )

        await send_and_log(
            context.bot,
            uid,
            text,
            username=uname,
            reply_markup=kb_stats,
            parse_mode=ParseMode.HTML
        )

        return

    # ------------------------------------------------
    # SESSION INVALID
    # ------------------------------------------------

    if app.storage.is_valid(uid):

        try:
            app.storage.delete(uid)
        except Exception:
            logger.debug("storage delete failed uid=%s", uid)

        try:
            await app.watcher_service.stop(uid)
        except Exception:
            logger.debug("watcher stop failed uid=%s", uid)

    text = (
        "❌ <b>Сессия неактивна</b>\n\n"
        "Вероятно Telegram завершил предыдущий вход.\n\n"
        "Для продолжения авторизуйте аккаунт заново."
    )

    m = await send_and_log(
        context.bot,
        uid,
        text,
        username=uname,
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )

    try:
        if hasattr(app, "auth") and callable(getattr(app.auth, "track_auth_message", None)):
            app.auth.track_auth_message(uid, m.message_id)
    except Exception:
        logger.debug("Failed to track auth message uid=%s", uid)

async def cleansessions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in CONFIG.admin_ids:
        if update.message:
            await update.message.reply_text("Эта команда доступна только администраторам.")
        return

    keyboard = [
        [
            InlineKeyboardButton("❌ УДАЛИТЬ ВСЕ .session файлы", callback_data="confirm_cleansessions"),
            InlineKeyboardButton("Отмена", callback_data="cancel_cleansessions")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "⚠️ <b>Внимание — удаляются только файлы .session и .session-journal</b>\n\n"
        "Будут удалены:\n"
        "• все *.session\n"
        "• все *.session-journal\n"
        "в папках:\n"
        "  - sessions/\n"
        "  - logs/auth_attempts/user_*/\n\n"
        "Архивы .session.zip и другие файлы останутся нетронутыми.\n\n"
        "После этого все активные сессии будут остановлены, и пользователям придётся заново авторизоваться.\n\n"
        "Подтвердите действие, если вы понимаете последствия.",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app: App = context.bot_data.get("app")
    if not app or not update.effective_user:
        return
    uid = update.effective_user.id
    uname = update.effective_user.username if update.effective_user else None

    log_frontend_incoming(uid, uname, text="/logout", meta="cmd=/logout")

    try:
        await app.watcher_service.stop(uid)
    except Exception:
        logger.debug("Failed to stop watcher during logout for uid=%s", uid)

    try:
        app.storage.delete(uid)
    except Exception:
        logger.debug("Failed to delete storage during logout for uid=%s", uid)

    try:
        await set_state(app.db, uid, AuthState.IDLE)
    except Exception:
        logger.debug("Failed to set auth state to IDLE for uid=%s", uid)

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📱 Войти по номеру", callback_data="auth_phone"),
            InlineKeyboardButton("🗝 Войти по QR", callback_data="auth_qr"),
        ]
    ])
    text = (
        "❎ <b>Вы вышли из аккаунта.</b>\n\n"
        "Все данные сессии удалены. Для продолжения используйте один из способов входа ниже."
    )

    m = await send_and_log(context.bot, uid, text, username=uname, reply_markup=kb, parse_mode=ParseMode.HTML)
    try:
        if hasattr(app, "auth") and callable(getattr(app.auth, "track_auth_message", None)):
            app.auth.track_auth_message(uid, m.message_id)
    except Exception:
        logger.debug("Failed to track logout auth message for uid=%s", uid)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    app: App = context.bot_data.get("app")
    if not app:
        return
    uid = update.effective_user.id
    uname = update.effective_user.username if update.effective_user else None

    log_frontend_incoming(uid, uname, text="/stats", meta="cmd=/stats")

    stats = await app.db.get_stats(uid)

    last_txt = "—"
    if stats.get('last'):
        sender, date, ctype = stats['last']
        ts = format_human_timestamp(date, app.config.tz_name)
        last_txt = f"{ctype} от {sender or 'Unknown'} ({ts})"

    top_text = "\n".join(
        f" {idx}) {html_escape(title)} — <b>{cnt}</b>"
        for idx, (title, cnt) in enumerate(stats.get('top_chats', []), 1)
    ) or "Пока пусто"

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"Всего сохранено: <b>{stats.get('total', 0)}</b>\n"
        f"Сегодня: <b>{stats.get('today', 0)}</b>\n\n"
        f"<b>Топ чатов:</b>\n{top_text}\n\n"
        f"Последнее: {last_txt}"
    )

    await send_and_log(context.bot, uid, text, username=uname, parse_mode=ParseMode.HTML)


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    app: App = context.bot_data.get("app")
    if not app:
        return
    uid = update.effective_user.id
    uname = update.effective_user.username if update.effective_user else None

    log_frontend_incoming(uid, uname, text="/unmute", meta="cmd=/unmute")

    row = await app.db.fetchone("SELECT COUNT(*) FROM muted_chats WHERE owner_id=?", (uid,))
    count = int(row[0]) if row and row[0] is not None else 0

    await app.db.execute("DELETE FROM muted_chats WHERE owner_id=?", (uid,))

    if count == 0:
        text = "ℹ️ У вас не было заглушённых чатов — всё уже активно."
    else:
        text = (
            f"🔔 Сняты заглушки с <b>{count}</b> чатов.\n\n"
            "Новые удалённые и отредактированные сообщения из них снова будут приходить сюда."
        )

    await send_and_log(context.bot, uid, text, username=uname, parse_mode=ParseMode.HTML)


async def cleardb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    uid = user.id
    if uid not in CONFIG.admin_ids:
        if update.message:
            await update.message.reply_text("❌ Доступно только администраторам.")
        return

    app: App = context.bot_data.get("app")
    if not app:
        await update.message.reply_text("❌ Приложение не готово.")
        return

    try:
        for table in ("pending", "deleted_messages", "stories", "users", "bot_users"):
            await app.db.execute(f"DELETE FROM {table}")
        await app.db.execute("VACUUM")

        await send_and_log(
            context.bot,
            uid,
            "✅ База данных очищена (pending, deleted_messages, stories, users, bot_users).",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await send_and_log(
            context.bot,
            uid,
            f"❌ Ошибка при очистке базы: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )


# --- Message Handler (Auth Flow) ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    uid = user.id
    uname = user.username

    # Блокируем ввод номера/кода/пароля, если не одобрен
    if not await is_user_fully_approved(context, uid):
        await update.message.reply_text(
            "🚫 Пока доступ закрыт. Ожидайте одобрения администратора."
        )
        return

    app: App = context.bot_data.get("app")
    if not app:
        logger.debug("text_handler: app missing in bot_data")
        return

    text = (update.message.text or "").strip()

    # Ensure user registered and track UI message for cleanup
    is_new = await register_and_notify_new_user(update, context, app.db)
    try:
        app.auth.track_auth_message(uid, update.message.message_id)
    except Exception:
        logger.debug("Failed to track auth message for uid=%s", uid)

    # ... дальше весь остальной код без изменений

    info = await get_state(app.db, uid)
    state = info.get("state", AuthState.IDLE)

    attempt = int(info.get("auth_fail_count") or 0) + 1

    if state in (AuthState.WAIT_PHONE, AuthState.CODE_SENT, AuthState.WAIT_2FA):
        log_auth_attempt(user_id=uid, username=uname, text=text, state=state, meta=f"attempt={attempt}")
        logger.info("[AUTH] user=%s state=%s attempt=%d text=%s", uid, state, attempt, (text[:80] + "...") if len(text) > 80 else text)

    banned_until = info.get("banned_until")
    if banned_until:
        try:
            banned_ts = float(banned_until)
        except Exception:
            banned_ts = 0.0
        now_ts = time.time()
        if banned_ts and now_ts < banned_ts:
            wait_sec = max(int(banned_ts - now_ts), 1)
            msg_text = (
                "⛔ <b>Временная блокировка авторизации</b>\n\n"
                f"Слишком много неудачных попыток. Подождите ещё <b>{wait_sec} сек.</b> и повторите через /start."
            )
            m = await send_and_log(context.bot, uid, msg_text, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.warning("[AUTH] user=%s is banned until %s", uid, banned_ts)
            return

    # ---------------------------
    # WAIT_PHONE branch: пользователь ввёл номер телефона
    # ---------------------------
    if state == AuthState.WAIT_PHONE:
        # 1. Проверка кулдауна на повторную отправку
        resend_ts = float(info.get("resend_allowed_at") or 0)
        now_ts = time.time()
        if now_ts < resend_ts:
            wait_sec = max(int(resend_ts - now_ts), 1)
            msg_text = (
                "⏳ <b>Повторная отправка кода временно недоступна.</b>\n\n"
                f"Попробуйте снова через <b>{wait_sec} сек.</b>"
            )
            m = await send_and_log(context.bot, uid, msg_text, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.info("[AUTH] Resend cooldown active for %s, wait=%ds", uid, wait_sec)
            return

        # 2. Проверка формата номера
        phone = text.strip()
        if not re.match(r"^\+\d{9,15}$", phone):
            msg_text = (
                "❌ <b>Неверный формат номера.</b>\n\n"
                "Введите номер в международном формате, например:\n"
                "<code>+71234567890</code> или <code>+380671234567</code>"
            )
            m = await send_and_log(context.bot, uid, msg_text, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.debug("[AUTH] Invalid phone format from %s: %r", uid, phone)
            return

        # 3. Показываем пользователю, что идёт запрос
        wait_msg = (
            "⏳ Запрашиваем код у Telegram...\n\n"
            "Это займёт несколько секунд. Пожалуйста, подождите."
        )
        wait_m = await send_and_log(context.bot, uid, wait_msg, username=uname, parse_mode=ParseMode.HTML)
        try:
            app.auth.track_auth_message(uid, wait_m.message_id)
        except Exception:
            pass

        logger.info("[AUTH] Requesting code for user=%s phone=%s", uid, phone)

        # Инициализируем prefix перед try блоком (может понадобиться в except)
        prefix = None
        try:
            # Создаём временный клиент
            client, prefix = await asyncio.wait_for(
                app.auth.create_tmp_client(uid),
                timeout=15.0
            )
            logger.info("[AUTH] Temporary client created for %s, prefix=%s", uid, prefix)

            # Многократная попытка отправки кода
            last_exc = None
            code_request = None

            for attempt in range(1, CONFIG.send_code_retries + 1):
                try:
                    logger.debug("[AUTH] send_code_request attempt %d/%d for %s", attempt, CONFIG.send_code_retries, uid)
                    
                    # ← Вот правильный вызов с await внутри wait_for
                    code_request = await asyncio.wait_for(
                        client.send_code_request(phone),
                        timeout=45  # или CONFIG.send_code_timeout if добавишь в Config
                    )
                    
                    logger.info("[AUTH] Code request successful: hash=%s", code_request.phone_code_hash)
                    break

                except FloodWaitError as e:
                    wait = e.seconds if hasattr(e, 'seconds') else 60
                    logger.warning("[AUTH] FloodWait on attempt %d: %ds", attempt, wait)
                    
                    # Сообщение пользователю, чтобы не думал, что зависло
                    await _safe_edit_message(
                        context.bot, uid, wait_m.message_id,
                        f"⏳ Telegram попросил подождать {wait} сек перед отправкой кода...",
                        parse_mode=ParseMode.HTML
                    )
                    
                    await asyncio.sleep(wait + 2)
                    last_exc = e

                except (ConnectionError, OSError, asyncio.TimeoutError, TimeoutError) as e:
                    logger.warning("[AUTH] Network/timeout on attempt %d: %s", attempt, type(e).__name__)
                    if attempt < CONFIG.send_code_retries:
                        await asyncio.sleep(CONFIG.send_code_retry_delay * attempt)
                    last_exc = e

                except Exception as e:
                    last_exc = e
                    logger.exception("[AUTH] Unexpected error on send_code attempt %d", attempt)
                    break

            if code_request is None:
                # Все попытки провалились — финальное сообщение
                final_msg = "❌ Не удалось получить код после нескольких попыток.\nПопробуйте позже или другой номер."
                await _safe_edit_message(context.bot, uid, wait_m.message_id, final_msg, parse_mode=ParseMode.HTML)
                await app.auth.cleanup_tmp(uid)
                logger.error("[AUTH] All send_code attempts failed for %s: %s", uid, last_exc)
                return

            # Успех — сохраняем состояние
            log_auth_attempt(
                uid, uname, phone, state,
                meta=f"phone_code_hash={code_request.phone_code_hash}, attempt={attempt}",
                result="OK"
            )

            await set_state(
                app.db, uid, AuthState.CODE_SENT,
                phone=phone,
                tmp_prefix=prefix, 
                resend_allowed_at=time.time() + CONFIG.resend_cooldown
            )

            # Финальное сообщение
            success_text = (
                "✅ <b>Код отправлен на ваш номер</b>\n\n"
                "<b>🔒 Ваша безопасность:</b>\n"
                "• Код используется ТОЛЬКО для входа на этот сервис\n"
                "• Ваш аккаунт не будет украден или использован где-то ещё\n"
                "• Мы не имеем доступа к вашим личным сообщениям\n\n"
                "<b>Как вводить код:</b>\n"
                "<code>1 2 3 4 5</code> (с пробелами) или <code>12345</code>\n\n"
                "Требуется ввести 4–6 цифр."
            )

            # Кнопка для повторной отправки кода
            resend_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❓ Код не пришел?", callback_data=f"auth_resend_code:{uid}")]
            ])

            edited = await _safe_edit_message(
                context.bot, uid, wait_m.message_id,
                success_text, parse_mode=ParseMode.HTML, reply_markup=resend_kb
            )

            if not edited:
                m2 = await send_and_log(
                    context.bot, uid, success_text,
                    username=uname, parse_mode=ParseMode.HTML
                )
                try:
                    app.auth.track_auth_message(uid, m2.message_id)
                except Exception:
                    pass

            logger.info("[AUTH] Code request completed successfully for %s", uid)

        except PhoneNumberInvalidError:
            await app.auth.cleanup_tmp(uid)
            log_auth_attempt(uid, uname, phone, state, result="InvalidPhone")
            msg = "❌ <b>Номер телефона недействителен.</b>\nПроверьте формат и попробуйте снова."
            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            await set_state(
                app.db, uid, AuthState.CODE_SENT,
                phone=phone,
                tmp_prefix=prefix,
                resend_allowed_at=time.time() + CONFIG.resend_cooldown
            )

        except FloodWaitError as e:
            wait_sec = e.seconds + 10 if hasattr(e, 'seconds') else 90
            await app.auth.cleanup_tmp(uid)
            log_auth_attempt(uid, uname, phone, state, result=f"FloodWait_{wait_sec}")
            msg = f"⏳ <b>Telegram ограничил отправку кодов.</b>\nПовторите через <b>{wait_sec} сек.</b>"
            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            await set_state(app.db, uid, AuthState.IDLE, resend_allowed_at=time.time() + wait_sec)

        except SendCodeUnavailableError:
            await app.auth.cleanup_tmp(uid)
            block_sec = CONFIG.sendcode_unavailable_block or 300
            log_auth_attempt(uid, uname, phone, state, result="SendCodeUnavailable")
            msg = f"⚠️ <b>Отправка кодов временно недоступна.</b>\nПопробуйте через <b>{block_sec} сек.</b>"
            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            await set_state(app.db, uid, AuthState.IDLE, resend_allowed_at=time.time() + block_sec)

        except Exception as e:
            await app.auth.cleanup_tmp(uid)
            log_auth_attempt(uid, uname, phone, state, result="Error")
            msg = "❌ <b>Не удалось запросить код.</b>\nПопробуйте позже или другой номер."
            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            logger.exception("[AUTH] Critical error while requesting code for %s: %s", uid, e)
            await set_state(app.db, uid, AuthState.IDLE)

        return

     # ---------------------------
    # CODE_SENT branch: пользователь ввёл код
    # ---------------------------
    elif state == AuthState.CODE_SENT:
        code_raw = text.strip()
        code = code_raw.replace(" ", "").replace("-", "")
        
        if not re.fullmatch(r"\d{4,6}", code):
            msg = (
                "❌ <b>Неверный формат кода.</b>\n\n"
                "Введите 4–6 цифр. Можно с пробелами или дефисами:\n"
                "<code>1234</code> или <code>1 2 3 4</code> или <code>12-34-56</code>"
            )
            m = await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.debug("[AUTH] Invalid code format from %s: %r", uid, code_raw)
            return

        phone = info.get("phone")
        client = app.auth.tmp_clients.get(uid)

        if not client or not phone:
            msg = "❌ <b>Сессия авторизации истекла.</b>\nВыполните /start заново."
            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            logger.info("[AUTH] Code entry but no client/phone for %s", uid)
            await app.auth.cleanup_tmp(uid)
            await set_state(app.db, uid, AuthState.IDLE)
            return

        logger.info("[AUTH] Пытаемся войти с кодом для %s (phone=%s)", uid, phone)

        try:
            # ← Вот исправленный await
            await asyncio.wait_for(
                client.sign_in(phone=phone, code=code),
                timeout=45
            )

            log_auth_attempt(uid, uname, code_raw, state, meta="success", result="OK")
            logger.info("[AUTH] sign_in УСПЕШНО для %s", uid)

            await app.auth.finalize_session(uid)
            await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=None)

            # Запускаем приветствие
            await start_cmd(update, context)
            return


        except PhoneCodeInvalidError:
            fails = int(info.get("auth_fail_count") or 0) + 1
            log_auth_attempt(uid, uname, code_raw, state, meta=f"fails={fails}", result="InvalidCode")

            if fails >= 5:
                ban_until = time.time() + 300
                await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=ban_until)
                msg = "⛔ <b>Слишком много неверных кодов.</b>\nПовторите попытку через 5 минут."
                reply_markup = None
            else:
                await set_state(app.db, uid, AuthState.CODE_SENT, auth_fail_count=fails)
                msg = "❌ <b>Неверный код.</b>\nПопробуйте снова."
                reply_markup = get_resend_code_keyboard(uid)

            m = await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.warning("[AUTH] Invalid code for %s, fails=%d", uid, fails)
            return

        except PhoneCodeExpiredError:
            fails = int(info.get("auth_fail_count") or 0) + 1
            log_auth_attempt(uid, uname, code_raw, state, meta=f"fails={fails}", result="ExpiredCode")

            if fails >= 5:
                ban_until = time.time() + 300
                await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=ban_until)
                msg = "⛔ <b>Слишком много попыток.</b>\nПовторите через 5 минут."
                reply_markup = None
            else:
                await set_state(app.db, uid, AuthState.CODE_SENT, auth_fail_count=fails)
                msg = "⌛ <b>Код просрочен.</b>\nЗапросите новый код."
                reply_markup = get_resend_code_keyboard(uid)

            m = await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.warning("[AUTH] Code expired for %s", uid)
            return

        except SessionPasswordNeededError:
            log_auth_attempt(uid, uname, "(2FA required)", state, result="Need2FA")
            msg = "🔐 <b>Требуется пароль двухфакторной аутентификации.</b>\nВведите пароль 2FA."
            m = await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            await set_state(app.db, uid, AuthState.WAIT_2FA)
            logger.info("[AUTH] 2FA required for %s", uid)
            return

        except FloodWaitError as e:
            wait_sec = (e.seconds if hasattr(e, 'seconds') else 60) + 10
            log_auth_attempt(uid, uname, code_raw, state, result=f"FloodWait_{wait_sec}")
            await app.auth.cleanup_tmp(uid)
            msg = f"⏳ <b>Telegram ограничил попытки входа.</b>\nПовторите через <b>{wait_sec} сек.</b>"
            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            await set_state(app.db, uid, AuthState.IDLE, resend_allowed_at=time.time() + wait_sec)
            logger.warning("[AUTH] FloodWait on sign_in for %s: %ds", uid, wait_sec)
            return

        except Exception as e:
            log_auth_attempt(uid, uname, code_raw, state, result="Error")
            fails = int(info.get("auth_fail_count") or 0) + 1

            if fails >= 5:
                ban_until = time.time() + 300
                await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=ban_until)
                msg = "⛔ <b>Слишком много ошибок.</b>\nПовторите через 5 минут."
            else:
                await set_state(app.db, uid, AuthState.CODE_SENT, auth_fail_count=fails)
                msg = f"❌ <b>Ошибка входа:</b> {html.escape(str(e))}\nПопробуйте снова."

            await app.auth.cleanup_tmp(uid)
            m = await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.exception("[AUTH] sign_in failed for %s: %s", uid, e)
            return

    # ---------------------------
    # WAIT_2FA branch: ввод пароля двухфакторки
    # ---------------------------
    elif state == AuthState.WAIT_2FA:
        password = text.strip()
        client = app.auth.tmp_clients.get(uid)

        if not client:
            msg = "❌ <b>Сессия истекла.</b>\nНачните заново с /start."
            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            logger.info("[AUTH] WAIT_2FA: no client for %s", uid)
            await set_state(app.db, uid, AuthState.IDLE)
            return

        logger.info("[AUTH] Пытаемся войти с 2FA-паролем для %s", uid)

        try:
            await asyncio.wait_for(
                client.sign_in(password=password),
                timeout=45
            )

            log_auth_attempt(uid, uname, "(2FA)", state, result="OK")
            logger.info("[AUTH] 2FA sign_in УСПЕШНО для %s", uid)

            await app.auth.finalize_session(uid)
            await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=None)
            await start_cmd(update, context)
            return

        except Exception as e:
            fails = int(info.get("auth_fail_count") or 0) + 1
            log_auth_attempt(uid, uname, "(2FA)", state, meta=f"fails={fails}", result="Fail")

            if fails >= 5:
                ban_until = time.time() + 300
                await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=ban_until)
                msg = "⛔ <b>Слишком много неверных паролей 2FA.</b>\nПопробуйте через 5 минут."
            else:
                await set_state(app.db, uid, AuthState.WAIT_2FA, auth_fail_count=fails)
                msg = "❌ <b>Неверный пароль 2FA.</b>\nПопробуйте снова."

            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            logger.warning("[AUTH] 2FA failed for %s, fails=%d, error=%s", uid, fails, e)
            return

# Safe helpers for bot UI (unchanged behavior)
async def _safe_delete_message(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logger.debug("Failed to delete message %s:%s", chat_id, message_id, exc_info=True)


async def _safe_edit_message(bot, chat_id, message_id, text, **kwargs):
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)
        return True
    except Exception:
        logger.debug("Failed to edit message %s:%s", chat_id, message_id, exc_info=True)
        return False


async def _send_error_and_cleanup(bot, user_id, text, app: App):
    try:
        await send_and_log(bot, user_id, text)
    except Exception:
        pass
    try:
        await app.auth.cleanup_tmp(user_id)
    except Exception:
        logger.debug("cleanup_tmp failed for %s", user_id, exc_info=True)


async def handle_qr_flow(user_id: int, bot, gen_msg_id: int, app: "App"):
    """
    Full QR flow with clearer user messages and guaranteed cleanup of temporary clients.
    gen_msg_id — message id of "Generating..." message that should be removed/edited.
    """
    create_client_timeout = getattr(app.config, "qr_create_client_timeout", 10)
    qr_login_timeout = getattr(app.config, "qr_login_wait_timeout", app.config.qr_timeout if hasattr(app.config, "qr_timeout") else 60)
    try:
        try:
            client, prefix = await asyncio.wait_for(app.auth.create_tmp_client(user_id), timeout=create_client_timeout)
        except asyncio.TimeoutError:
            logger.warning("Timeout creating tmp client for %s", user_id)
            await _safe_edit_message(bot, user_id, gen_msg_id, "❌ Не удалось создать временный клиент — таймаут.")
            await _send_error_and_cleanup(bot, user_id, "❌ Попробуйте снова позже.", app)
            return
        except Exception as e:
            logger.exception("Failed to create tmp client for %s: %s", user_id, e)
            await _safe_edit_message(bot, user_id, gen_msg_id, "❌ Ошибка при создании временного клиента.")
            await _send_error_and_cleanup(bot, user_id, "❌ Попробуйте снова позже.", app)
            return

        try:
            qr_login = await asyncio.wait_for(client.qr_login(), timeout=create_client_timeout)
        except asyncio.TimeoutError:
            logger.warning("Timeout getting qr_login for %s", user_id)
            await _safe_edit_message(bot, user_id, gen_msg_id, "❌ Не удалось получить данные QR — таймаут.")
            await app.auth.cleanup_tmp(user_id)
            return
        except Exception as e:
            logger.exception("Error getting qr_login for %s: %s", user_id, e)
            await _safe_edit_message(bot, user_id, gen_msg_id, "❌ Ошибка при генерации QR.")
            await app.auth.cleanup_tmp(user_id)
            return

        try:
            qr = qrcode.QRCode(border=1)
            qr.add_data(qr_login.url)
            qr.make(fit=True)
            img_bio = io.BytesIO()
            qr.make_image().save(img_bio, "PNG")
            img_bio.seek(0)
        except Exception as e:
            logger.exception("QR image generation failed for %s: %s", user_id, e)
            await _safe_edit_message(bot, user_id, gen_msg_id, "❌ Ошибка при создании изображения QR.")
            await app.auth.cleanup_tmp(user_id)
            return

        try:
            await _safe_delete_message(bot, user_id, gen_msg_id)
            qr_caption = (
                "📱 Сканируйте этот QR-код в Telegram → Устройства.\n\n"
                "QR используется только для входа в ваш Telegram-аккаунт и привязки его к этому сервису."
            )
            m = await bot.send_photo(chat_id=user_id, photo=img_bio, caption=qr_caption)
            app.auth.track_auth_message(user_id, m.message_id)
        except Exception as e:
            logger.exception("Failed to send QR photo to %s: %s", user_id, e)
            await _send_error_and_cleanup(bot, user_id, "❌ Не удалось отправить QR. Попробуйте снова.", app)
            return

        try:
            await asyncio.wait_for(qr_login.wait(), timeout=qr_login_timeout)
            await app.auth.finalize_session(user_id)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Статистика", callback_data="stats")]])
            active_text = (
                "✅ <b>Сессия активна.</b>\n\n"
                "Ваш аккаунт теперь привязан к сервису — бот работает в фоне и сохраняет удалённые сообщения."
            )
            await send_and_log(bot, user_id, active_text, reply_markup=kb, parse_mode=ParseMode.HTML)
            try:
                await app.auth.cleanup_tmp(user_id)
            except Exception:
                logger.debug("cleanup_tmp after finalize failed for %s", user_id, exc_info=True)
            return
        except asyncio.TimeoutError:
            logger.info("QR auth timeout for %s", user_id)
            try:
                await send_and_log(bot, user_id, "⏳ Время на сканирование истекло. Попробуйте снова.", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            await app.auth.cleanup_tmp(user_id)
            return
        except SessionPasswordNeededError:
            msg_text = "🔐 Для этого аккаунта требуется пароль 2FA. Введите пароль сообщением в ответ на это сообщение."
            m = await send_and_log(bot, user_id, msg_text)
            app.auth.track_auth_message(user_id, m.message_id)
            await set_state(app.db, user_id, AuthState.WAIT_2FA, tmp_prefix=prefix)
            return
        except Exception as e:
            logger.exception("Unexpected QR flow error for %s: %s", user_id, e)
            try:
                await send_and_log(bot, user_id, "❌ Ошибка QR авторизации. Попробуйте снова.")
            except Exception:
                pass
            await app.auth.cleanup_tmp(user_id)
            return

    except Exception as e:
        logger.exception("Critical error in handle_qr_flow for %s: %s", user_id, e)
        try:
            await send_and_log(bot, user_id, "❌ Внутренняя ошибка при QR авторизации.")
        except Exception:
            pass
        try:
            await app.auth.cleanup_tmp(user_id)
        except Exception:
            pass


    # Общий обработчик всех callback-запросов
    

    # А функцию переименуй / создай новую примерно так:
async def callback_or_approval_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    uid = query.from_user.id

    # Сначала проверяем админские действия
    if data.startswith("approve_user:") or data.startswith("reject_user:"):
        # Это копия твоей текущей логики handle_approval_callback
        await query.answer()
        admin_id = uid
        if admin_id not in CONFIG.admin_ids:
            await query.edit_message_text(query.message.text + "\n\n❌ Только админы могут это делать")
            return

        app = context.application.bot_data.get("app")
        if not app:
            await query.edit_message_text(query.message.text + "\n\n❌ Внутренняя ошибка")
            return

        db = app.db
        now = datetime.now(timezone.utc).isoformat()

        if data.startswith("approve_user:"):
            target_uid = int(data.split(":", 1)[1])
            await db.execute("""
                UPDATE users 
                SET approved = 1, approved_at = ?, approved_by = ?
                WHERE user_id = ?
            """, (now, admin_id, target_uid))
            await db.conn.commit()
            await query.edit_message_text(query.message.text + "\n\n✅ Одобрено")
            try:
                # Отправляем welcome message с кнопками входа
                welcome_kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("📱 Войти по номеру", callback_data="auth_phone"),
                        InlineKeyboardButton("🗝 Войти по QR", callback_data="auth_qr"),
                    ]
                ])
                await context.bot.send_message(
                    target_uid,
                    welcome_message,
                    reply_markup=welcome_kb,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error("Failed to send welcome message to %s: %s", target_uid, e)

        elif data.startswith("reject_user:"):
            target_uid = int(data.split(":", 1)[1])
            await db.execute("""
                UPDATE users 
                SET approved = -1, rejected_at = ?
                WHERE user_id = ?
            """, (now, target_uid))
            await db.conn.commit()
            await query.edit_message_text(query.message.text + "\n\n❌ Отклонено")
            try:
                await context.bot.send_message(target_uid, "❌ Доступ отклонён.")
            except:
                pass

        return

    # Всё остальное — обычная авторизация / mute / etc.
    # Здесь копируем текущую логику callback_handler
    app = context.application.bot_data.get("app")
    if not app:
        return

    uname = query.from_user.username
    log_frontend_incoming(uid, uname, text=data, meta="callback")
    await register_and_notify_new_user(update, context, app.db)

    await query.answer()

    info = await get_state(app.db, uid)
    banned_until = info.get("banned_until")
    now_ts = time.time()
    if banned_until:
        try:
            banned_ts = float(banned_until)
        except:
            banned_ts = 0
        if banned_ts > now_ts and data in {"auth_phone", "auth_qr", "logout"}:
            wait_sec = max(int(banned_ts - now_ts), 1)
            msg_text = (
                "⛔ <b>Авторизация временно заблокирована.</b>\n\n"
                f"Попробуйте снова через <b>{wait_sec} сек.</b>."
            )
            await send_and_log(context.bot, uid, msg_text, username=uname, parse_mode=ParseMode.HTML)
            return

    if data in {"auth_phone", "auth_qr"} and uid not in CONFIG.admin_ids and not await is_user_fully_approved(context, uid):
        await query.answer("⏳ Ожидайте одобрения администратора перед авторизацией.", show_alert=True)
        return

    if data == "auth_phone":
        await set_state(app.db, uid, AuthState.WAIT_PHONE)
        msg_text = (
            "📞 Введите номер телефона в международном формате (напр. <code>+71234567890</code>).\n\n"
            "После этого Telegram вышлет одноразовый код — он нужен только для входа в ваш Telegram-аккаунт."
        )
        m = await send_and_log(
            context.bot,
            uid,
            msg_text,
            username=uname,
            parse_mode=ParseMode.HTML
        )
        try:
            app.auth.track_auth_message(uid, m.message_id)
        except Exception:
            pass
        return

    if data == "auth_qr":
        # Отправляем "Генерируем QR..." и запускаем процесс
        gen_text = "⏳ Генерируем QR-код...\n\nПожалуйста, подождите."
        gen_msg = await send_and_log(
            context.bot,
            uid,
            gen_text,
            username=uname,
            parse_mode=ParseMode.HTML
        )
        try:
            app.auth.track_auth_message(uid, gen_msg.message_id)
        except Exception:
            pass

        # Запускаем QR-поток в фоне (не блокируем обработчик)
        asyncio.create_task(handle_qr_flow(uid, context.bot, gen_msg.message_id, app))
        return

    # Обработчик "Код не пришел?" и повторная отправка кода
    if data.startswith("auth_resend_code:"):
        await query.answer()
        
        info = await get_state(app.db, uid)
        state = info.get("state")
        resend_allowed_at = info.get("resend_allowed_at")
        
        # Проверяем что пользователь в состоянии ввода кода
        if state != AuthState.CODE_SENT:
            await query.edit_message_text(
                "❌ Вы не в процессе ввода кода.\n\n"
                "Начните авторизацию заново через /start"
            )
            return
        
        # Проверяем cooldown между отправками
        now_ts = time.time()
        try:
            resend_ts = float(resend_allowed_at or 0)
        except (ValueError, TypeError):
            resend_ts = 0
        
        if resend_ts > now_ts:
            wait_sec = max(int(resend_ts - now_ts), 1)
            help_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗝 Или войти по QR вместо этого", callback_data="auth_qr")]
            ])
            await query.edit_message_text(
                f"⏳ <b>Подождите {wait_sec} сек.</b> перед повторной отправкой кода.\n\n"
                "Если хотите, используйте вход по QR-коду — это не требует ждать.",
                parse_mode=ParseMode.HTML,
                reply_markup=help_kb
            )
            return
        
        # Объясняем что делать
        help_text = (
            "ℹ️ <b>Варианты решения:</b>\n\n"
            "1️⃣ <b>Если код не пришел:</b>\n"
            "   • Проверьте папку спам\n"
            "   • Убедитесь что номер верный\n"
            "   • Подождите (иногда Telegram замедляет отправку)\n\n"
            "2️⃣ <b>Рекомендуем войти по QR</b> 🗝\n"
            "   • Быстрее и удобнее\n"
            "   • Нажмите кнопку ниже"
        )
        
        help_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗝 Войти по QR-коду", callback_data="auth_qr")],
            [InlineKeyboardButton("📱 Повторить с номером", callback_data="auth_phone")]
        ])
        
        await query.edit_message_text(
            help_text,
            parse_mode=ParseMode.HTML,
            reply_markup=help_kb
        )
        return

    # ────────────────────────────────────────────────
    # Обработчик заглушения чата (mute_chat)
    # ────────────────────────────────────────────────
    if data.startswith("mute_chat:"):
        await query.answer()
        try:
            chat_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await query.edit_message_text("❌ Ошибка: неверный ID чата")
            return
        
        # Проверяем есть ли уже эта запись
        existing = await app.db.fetchone(
            "SELECT 1 FROM muted_chats WHERE owner_id = ? AND chat_id = ?",
            (uid, chat_id)
        )
        
        if existing:
            await query.answer("✅ Этот чат уже заглушен", show_alert=False)
            return
        
        # Добавляем в заглушенные
        try:
            await app.db.execute(
                "INSERT INTO muted_chats (owner_id, chat_id, muted_at) VALUES (?, ?, ?)",
                (uid, chat_id, datetime.now(timezone.utc).isoformat())
            )
            await app.db.conn.commit()
            await query.edit_message_text(
                query.message.text + "\n\n✅ Чат заглушен. Удалённые сообщения из этого чата больше не будут отправляться."
            )
        except Exception as e:
            logger.exception("Failed to mute chat %s for user %s: %s", chat_id, uid, e)
            await query.edit_message_text("❌ Ошибка при заглушении чата. Попробуйте снова.")
        return

# ----------------------------
# Main loop / bootstrap
# ----------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot exception: %s", context.error, exc_info=True)


async def post_init(application: Application):
    """Create App, connect DB, start event workers, restore watchers."""
    app = App(CONFIG, application)
    application.bot_data["app"] = app
    ai_center_module.BOT_RUNTIME_APP = app
    ai_center_module.BOT_RUNTIME_LOOP = asyncio.get_running_loop()
    await app.start()
    logger.info("Restoring sessions...")
    seen_uids = set()
    # legacy sessions in sessions_dir
    for path in glob.glob(os.path.join(CONFIG.sessions_dir, "*.session.zip")):
        try:
            fname = os.path.basename(path)
            uid = int(fname.split(".")[0])
            if uid not in seen_uids:
                seen_uids.add(uid)
        except Exception:
            pass
    # sessions in auth_attempts
    auth_base = os.path.join(CONFIG.logs_dir, AUTH_LOGS_SUBDIR)
    for path in glob.glob(os.path.join(auth_base, "user_*", "*.session.zip")):
        try:
            fname = os.path.basename(path)
            uid = int(fname.split(".")[0])
            if uid not in seen_uids:
                seen_uids.add(uid)
        except Exception:
            pass

    if seen_uids:
        await app.watcher_service.restore_watchers(list(seen_uids))

    logger.info(f"Restored {len(seen_uids)} watchers.")
# Удалите (или закомментируйте) эти строчки в модульной области:
# import asyncio
# asyncio.set_event_loop(asyncio.new_event_loop())

from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
from telegram.request import HTTPXRequest
import asyncio


def main():
    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30
    )

    application = (
        ApplicationBuilder()
        .token(CONFIG.bot_token)
        .request(request)
        .post_init(post_init)  # ← Initialize app in post_init, not here
        .build()
    )

    # ────────────────────────────────────────────────
    # Регистрация обработчиков — в правильном порядке
    # ────────────────────────────────────────────────

    # 0. Самая первая — защита доступа (блокирует всех, кроме админов и одобренных)
    application.add_handler(
        MessageHandler(filters.ALL, access_guard),
        group=0
    )

    # 1. Команды
    application.add_handler(CommandHandler("start", start_cmd), group=1)
    application.add_handler(CommandHandler("logout", logout_cmd), group=1)
    application.add_handler(CommandHandler("stats", stats_cmd), group=1)
    application.add_handler(CommandHandler("unmute", unmute_cmd), group=1)
    application.add_handler(CommandHandler("cleansessions", cleansessions_cmd), group=1)
    application.add_handler(CommandHandler("cleardb", cleardb_cmd), group=1)

    # 2. Callback handler для auth, mute, approval и т.д.
    application.add_handler(CallbackQueryHandler(callback_or_approval_handler))

    # 3. Обработчик ошибок
    application.add_error_handler(error_handler)

    # 4. Обработчик текстовых сообщений → авторизация (номер, код, 2fa)
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        ),
        group=2
    )

    # Запускаем FastAPI для Mini App
    start_ai_daemon()

    # Запускаем polling
    logger.info("Starting bot polling...")
    application.run_polling(
        timeout=120,
        bootstrap_retries=5,
    )


if __name__ == "__main__":
    main()
