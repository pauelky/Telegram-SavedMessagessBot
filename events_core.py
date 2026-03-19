from .shared import *

class DeleteAggregator:
    """
    Aggregator that forwards saved/edited/deleted message payloads to bot owners.
    """

    def __init__(self, bot_app, db, *, workers: int = 2):
        self.bot = bot_app.bot
        self.db = db
        self.send_queue: asyncio.Queue = asyncio.Queue()
        self.workers = max(1, int(workers))
        self._workers_tasks: List[asyncio.Task] = []
        self._stopping = False

    async def start_workers(self) -> None:
        if self._workers_tasks:
            return
        self._stopping = False
        for _ in range(self.workers):
            t = asyncio.create_task(self._send_worker())
            self._workers_tasks.append(t)
        try:
            logger.info("DeleteAggregator: started %d workers", len(self._workers_tasks))
        except Exception:
            pass

    async def stop_workers(self) -> None:
        self._stopping = True
        for t in list(self._workers_tasks):
            t.cancel()
        for t in list(self._workers_tasks):
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                try:
                    logger.exception("DeleteAggregator worker shutdown error")
                except Exception:
                    pass
        self._workers_tasks = []
        try:
            logger.info("DeleteAggregator: stopped workers")
        except Exception:
            pass

    async def add_event(self, owner_id: int, payload: Dict[str, Any]) -> None:
        await self.send_queue.put((owner_id, payload))

    async def _send_worker(self) -> None:
        while not self._stopping:
            try:
                owner_id, payload = await self.send_queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._send_single_with_retry(owner_id, payload)
            except Exception:
                try:
                    logger.exception("DeleteAggregator worker failed to send payload")
                except Exception:
                    pass
            finally:
                try:
                    self.send_queue.task_done()
                except Exception:
                    pass
        while not self.send_queue.empty():
            try:
                self.send_queue.get_nowait()
                self.send_queue.task_done()
            except Exception:
                break

    async def _send_single_with_retry(self, owner_id: int, payload: Dict[str, Any], *, max_attempts: int = 6):
        """
        Send message with retry logic and pre-flight checks.
        Pre-flight checks ensure we don't waste attempts on impossible cases.
        """
        # ─── PRE-FLIGHT CHECKS (before any retry attempt) ───
        
        # 1. Check if user is banned
        try:
            user_row = await self.db.fetchone(
                "SELECT banned FROM bot_users WHERE user_id=?",
                (owner_id,)
            )
            if user_row and user_row[0]:  # User is banned
                logger.debug("User %s is banned, skipping send", owner_id)
                return
        except Exception:
            logger.debug("Could not check ban status for user %s", owner_id)
            # Continue anyway; they might not be registered yet
        
        # 2. Check if chat is muted (before sending anything)
        chat_id = payload.get("chat_id")
        if chat_id:
            try:
                is_muted = await self.db.fetchone(
                    "SELECT 1 FROM muted_chats WHERE owner_id=? AND chat_id=?",
                    (owner_id, chat_id)
                )
                if is_muted:
                    logger.debug("Chat %s is muted for user %s, skipping send", chat_id, owner_id)
                    return
            except Exception:
                logger.debug("Could not check mute status for user %s / chat %s", owner_id, chat_id)
        
        # ─── RETRY LOOP ───
        attempt = 0
        base_delay = 0.6
        while True:
            attempt += 1
            try:
                return await self._send_single(owner_id, payload)
            except RetryAfter as e:
                wait = float(getattr(e, "retry_after", 1.0)) + 0.3
                try:
                    logger.warning("DeleteAggregator: RetryAfter, sleeping %.1fs (attempt %d)", wait, attempt)
                except Exception:
                    pass
                await asyncio.sleep(wait)
            except (TimedOut, NetworkError) as e:
                if attempt >= max_attempts:
                    try:
                        logger.exception("DeleteAggregator: Network error and max attempts reached")
                    except Exception:
                        pass
                    raise
                sleep_for = min(base_delay * (2 ** (attempt - 1)), 10.0)
                await asyncio.sleep(sleep_for)
            except Exception:
                if attempt >= max_attempts:
                    try:
                        logger.exception("DeleteAggregator: Unexpected error and max attempts reached")
                    except Exception:
                        pass
                    raise
                sleep_for = min(base_delay * (2 ** (attempt - 1)), 10.0)
                await asyncio.sleep(sleep_for)

    async def _read_file_bytes(self, path: str) -> bytes:
        loop = asyncio.get_running_loop()
        def _read():
            with open(path, "rb") as f:
                return f.read()
        return await loop.run_in_executor(None, _read)

    async def _send_single(self, owner_id: int, item: Dict[str, Any]) -> Optional[Any]:
        media_path: Optional[str] = item.get("media_path")
        text: str = (item.get("text") or "")[:50000]
        original_text: str = (item.get("original_text") or "")[:50000]
        edit_count: int = int(item.get("edit_count") or 0)
        event_type: str = item.get("event_type", "deleted")
        sender: str = item.get("sender_username") or ("ID " + str(item.get("sender_id", "")))
        chat: str = item.get("chat_title") or "—"
        chat_id = item.get("chat_id")
        chat_username = item.get("chat_username") or None
        content_type = (item.get("content_type") or "")

        ts_del = format_human_timestamp(item.get("deleted_at"), CONFIG.tz_name)
        ts_edit = format_human_timestamp(item.get("edited_at"), CONFIG.tz_name) if item.get("edited_at") else None
        ts_orig = format_human_timestamp(item.get("message_date"), CONFIG.tz_name)

        def _clip(s: str, n: int) -> str:
            s = s or ""
            return s if len(s) <= n else (s[: max(0, n - 3)] + "...")

        block_limit = 300 if (media_path and os.path.exists(media_path)) else 3000

        sender_h = html.escape(sender)
        chat_h = html.escape(chat)
        text_preview = html.escape(_clip(text, block_limit))
        original_preview = html.escape(_clip(original_text, block_limit))

        # ────────────────────────────────────────────────
        # ОПРЕДЕЛЯЕМ ЗАГОЛОВОК И ТИП КОНТЕНТА
        # ────────────────────────────────────────────────
        if event_type == "disappearing":
            title = "👻 <b>Исчезающее сообщение</b>"
            context_text = "было отправлено с таймером самоуничтожения"
            content_display = "💬 Содержание"

        elif event_type == "edited":
            title = "✏️ <b>Сообщение отредактировано</b>"
            context_text = "был отредактирован"
            content_display = "✏️ Изменения"

        elif event_type == "story":
            if "Video" in (content_type or ""):
                title = "🎬 <b>Видео-история сохранена</b>"
                context_text = "опубликовал видео-историю"
            else:
                title = "📷 <b>История сохранена</b>"
                context_text = "опубликовал историю"
            content_display = "📸 История"

        else:  # deleted
            title = "🗑️ <b>Удалённое сообщение</b>"
            context_text = "было удалено из чата"
            content_display = "📝 Содержание"

        # ────────────────────────────────────────────────
        # ФОРМИРУЕМ ИНФОРМАЦИОННОЕ СООБЩЕНИЕ
        # ────────────────────────────────────────────────
        meta_parts: List[str] = []
        meta_parts.append(title)
        meta_parts.append("")  # пустая строка для разделения
        
        # Основная информация о событии
        sender_id = item.get("sender_id")
        if sender_id:
            sender_info = f"@{sender_h} <code>ID:{sender_id}</code>"
        else:
            sender_info = f"@{sender_h}"
        
        meta_parts.append(f"👤 <b>От кого:</b> {sender_info}")
        meta_parts.append(f"💬 <b>В чате:</b> {chat_h}")
        meta_parts.append(f"⏰ <b>Время события:</b> {ts_orig}")
        
        # Дополнительные временные метки
        if event_type == "deleted":
            meta_parts.append(f"❌ <b>Удалено:</b> {ts_del}")
        elif event_type == "edited":
            meta_parts.append(f"✏️ <b>Отредактировано:</b> {ts_edit or ts_orig}")
            if edit_count > 0:
                meta_parts.append(f"📊 <b>Всего изменений:</b> {edit_count}")
        
        # Тип контента
        content_type_display = content_type or guess_content_type_from_path(media_path) or "сообщение"
        meta_parts.append(f"📄 <b>Тип:</b> {html.escape(content_type_display)}")
        
        # Статистика для удаленных сообщений
        if event_type == "deleted":
            views = item.get("views", 0)
            if views and views > 0:
                meta_parts.append(f"👁️ <b>Просмотров:</b> {views}")
            
            reactions_json = item.get("reactions", "{}")
            reactions_display = format_reactions_display(reactions_json)
            if reactions_display:
                meta_parts.append(f"❤️ <b>Реакции:</b> {reactions_display}")

        meta_text = "\n".join(meta_parts)

        # ────────────────────────────────────────────────
        # КНОПКИ ДЛЯ ИНТЕРАКТИВНОСТИ
        # ────────────────────────────────────────────────
        buttons: List[List[InlineKeyboardButton]] = []
        button_row: List[InlineKeyboardButton] = []
        
        # Кнопка для перехода в чат (всегда как URL)
        if chat_username:
            try:
                url = f"https://t.me/{chat_username.lstrip('@')}"
                button_row.append(InlineKeyboardButton("↗️ Перейти в чат", url=url))
            except Exception:
                pass
        elif chat_id and chat_id > 0:
            # Для приватных групп используем t.me/c/chat_id
            try:
                # chat_id обычно отрицательный для групп, преобразуем в положительный
                positive_id = abs(int(chat_id or 0))
                url = f"https://t.me/c/{positive_id}"
                button_row.append(InlineKeyboardButton("↗️ Перейти в чат", url=url))
            except Exception:
                pass
        
        if button_row:
            buttons.append(button_row)

        meta_reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

        # ────────────────────────────────────────────────
        # Логирование отправляемого сообщения
        # ────────────────────────────────────────────────
        try:
            log_outgoing_message(owner_id, meta_text)
        except Exception:
            try:
                logger.exception("Failed to log outgoing message for owner %s", owner_id)
            except Exception:
                pass

        # ────────────────────────────────────────────────
        # ОТПРАВКА СООБЩЕНИЯ 1: МЕТ А
        # ────────────────────────────────────────────────
        try:
            meta_msg = await self.bot.send_message(
                chat_id=owner_id,
                text=meta_text,
                parse_mode=ParseMode.HTML,
                reply_markup=meta_reply_markup
            )
        except Exception:
            try:
                logger.exception("Failed to send meta message to owner %s", owner_id)
            except Exception:
                pass
            # Если даже мета не отправилась, прерываем
            raise

        # Небольшая задержка между сообщениями для сохранения порядка
        await asyncio.sleep(0.3)

        # ────────────────────────────────────────────────
        # СООБЩЕНИЕ 2: КОНТЕНТ (медиа или текст)
        # ────────────────────────────────────────────────
        if event_type == "story":
            # Сторис — специальный случай
            # СНАЧАЛА МЕДИА, ПОТОМ ТЕКСТ (если есть)
            if media_path and os.path.exists(media_path):
                ext = os.path.splitext(media_path)[1].lower()
                bio: Optional[io.BytesIO] = None
                story_media_msg = None
                try:
                    data = await self._read_file_bytes(media_path)
                    bio = io.BytesIO(data)
                    bio.name = os.path.basename(media_path)
                    bio.seek(0)

                    if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp") or "photo" in content_type.lower():
                        story_media_msg = await self.bot.send_photo(chat_id=owner_id, photo=bio, parse_mode=ParseMode.HTML)
                    elif ext in (".mp4", ".mov") or "video" in content_type.lower():
                        story_media_msg = await self.bot.send_video(chat_id=owner_id, video=bio, parse_mode=ParseMode.HTML, supports_streaming=True)
                    else:
                        story_media_msg = await self.bot.send_document(chat_id=owner_id, document=bio, parse_mode=ParseMode.HTML)
                except Exception:
                    try:
                        logger.exception("Failed to send story media to owner %s", owner_id)
                    except Exception:
                        pass
                finally:
                    if bio:
                        try:
                            bio.close()
                        except Exception:
                            pass
                
                # Отправляем текст/caption ОТДЕЛЬНЫМ сообщением после медиа
                if text and text.strip():
                    try:
                        await asyncio.sleep(0.2)  # Задержка между медиа и текстом
                        return await self.bot.send_message(
                            chat_id=owner_id,
                            text=text_preview,
                            parse_mode=ParseMode.HTML
                        )
                    except Exception:
                        try:
                            logger.exception("Failed to send story text after media to owner %s", owner_id)
                        except Exception:
                            pass
                
                return story_media_msg
            else:
                # Сторис без медиа — отправляем как текст с подписью
                if text and text.strip():
                    try:
                        return await self.bot.send_message(
                            chat_id=owner_id,
                            text=text_preview,
                            parse_mode=ParseMode.HTML
                        )
                    except Exception:
                        try:
                            logger.exception("Failed to send story text to owner %s", owner_id)
                        except Exception:
                            pass
                        raise
                else:
                    # Story без текста и без медиа - отправляем placeholder
                    try:
                        return await self.bot.send_message(
                            chat_id=owner_id,
                            text="📖 (сторис без контента)",
                            parse_mode=ParseMode.HTML
                        )
                    except Exception:
                        try:
                            logger.exception("Failed to send story placeholder to owner %s", owner_id)
                        except Exception:
                            pass
                        raise

        else:
            # Обычные сообщения (deleted, edited, disappearing)
            # ────────────────────────────────────────────────
            # Сборка контента для отправки
            # ────────────────────────────────────────────────
            content_parts: List[str] = []
            
            if event_type == "edited" and edit_count > 0 and (original_text != text):
                content_parts.append(f"<b>Редактировалось:</b> {edit_count} раз(а)\n")
                content_parts.append(f"<b>Было:</b>\n{original_preview}\n")
                content_parts.append(f"<b>Стало:</b>\n{text_preview}")
            else:
                # Обычный текст
                if text.strip():
                    content_parts.append(text_preview)
            
            # Реакции и просмотры (для удалённых)
            if event_type == "deleted":
                reactions_json = item.get("reactions", "{}")
                reactions_display = format_reactions_display(reactions_json)
                if reactions_display:
                    if content_parts:
                        content_parts.append("\n")
                    content_parts.append(f"<b>Реакции:</b> {reactions_display}")
                
                views = item.get("views", 0)
                if views is not None and views > 0:
                    if content_parts:
                        content_parts.insert(0, "\n")
                    content_parts.insert(0, f"<b>Просмотры:</b> {views}")

            content_text = "".join(content_parts)

            # ────────────────────────────────────────────────
            # Отправка контента: СНАЧАЛА МЕДИА, ПОТОМ ТЕКСТ
            # ────────────────────────────────────────────────
            media_msg = None
            if media_path and os.path.exists(media_path):
                # Отправляем медиа
                ext = os.path.splitext(media_path)[1].lower()
                bio: Optional[io.BytesIO] = None
                try:
                    data = await self._read_file_bytes(media_path)
                    bio = io.BytesIO(data)
                    bio.name = os.path.basename(media_path)
                    bio.seek(0)

                    ctype = (content_type or "").lower()

                    if "video_note" in ctype or "кружочек" in ctype:
                        media_msg = await self.bot.send_video_note(chat_id=owner_id, video_note=bio, parse_mode=ParseMode.HTML)
                    elif ext in (".mp4", ".mov") or "video" in ctype or "видео" in ctype:
                        media_msg = await self.bot.send_video(chat_id=owner_id, video=bio, parse_mode=ParseMode.HTML, supports_streaming=True)
                    elif ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp") or "photo" in ctype or "image" in ctype:
                        media_msg = await self.bot.send_photo(chat_id=owner_id, photo=bio, parse_mode=ParseMode.HTML)
                    elif ext in (".ogg", ".oga", ".mp3", ".wav") or "voice" in ctype or "audio" in ctype:
                        if ext in (".ogg", ".oga"):
                            media_msg = await self.bot.send_voice(chat_id=owner_id, voice=bio, parse_mode=ParseMode.HTML)
                        else:
                            media_msg = await self.bot.send_audio(chat_id=owner_id, audio=bio, parse_mode=ParseMode.HTML)
                    else:
                        media_msg = await self.bot.send_document(chat_id=owner_id, document=bio, parse_mode=ParseMode.HTML)

                except Exception:
                    try:
                        logger.exception("Failed to send media to owner %s from %s", owner_id, media_path)
                    except Exception:
                        pass
                    # Фолбэк на текст если медиа не отправилась
                    if content_text:
                        try:
                            truncated = content_text if len(content_text) <= 3800 else content_text[:3797] + "..."
                            return await self.bot.send_message(chat_id=owner_id, text=truncated, parse_mode=ParseMode.HTML)
                        except Exception:
                            try:
                                logger.exception("Failed fallback send to owner %s", owner_id)
                            except Exception:
                                pass
                            raise
                finally:
                    if bio:
                        try:
                            bio.close()
                        except Exception:
                            pass

                # Отправляем текст ОТДЕЛЬНЫМ сообщением после медиа
                if content_text:
                    try:
                        await asyncio.sleep(0.2)  # Задержка между медиа и текстом
                        if len(content_text) <= 3800:
                            return await self.bot.send_message(chat_id=owner_id, text=content_text, parse_mode=ParseMode.HTML)
                        else:
                            # Длинный текст — компактная версия + файл
                            compact = content_text[:3797] + "..."
                            await self.bot.send_message(chat_id=owner_id, text=compact, parse_mode=ParseMode.HTML)
                            await asyncio.sleep(0.2)
                            
                            loop = asyncio.get_running_loop()
                            def _make_bytes():
                                return (text or "").encode("utf-8")
                            full_bytes = await loop.run_in_executor(None, _make_bytes)

                            bio = io.BytesIO(full_bytes)
                            bio.name = f"message_{item.get('msg_id','unknown')}.txt"
                            bio.seek(0)
                            try:
                                return await self.bot.send_document(chat_id=owner_id, document=bio, filename=bio.name, parse_mode=ParseMode.HTML)
                            finally:
                                try:
                                    bio.close()
                                except Exception:
                                    pass
                    except Exception:
                        try:
                            logger.exception("Failed to send text after media for owner %s", owner_id)
                        except Exception:
                            pass
                return media_msg

            else:
                # Только текст, без медиа
                try:
                    if content_text:
                        if len(content_text) <= 3800:
                            return await self.bot.send_message(chat_id=owner_id, text=content_text, parse_mode=ParseMode.HTML)

                        # Длинный текст — отправляем укороченный + файл
                        compact = content_text[:3797] + "..."
                        await self.bot.send_message(chat_id=owner_id, text=compact, parse_mode=ParseMode.HTML)

                        loop = asyncio.get_running_loop()
                        def _make_bytes():
                            return (text or "").encode("utf-8")
                        full_bytes = await loop.run_in_executor(None, _make_bytes)

                        bio = io.BytesIO(full_bytes)
                        bio.name = f"message_{item.get('msg_id','unknown')}.txt"
                        bio.seek(0)
                        try:
                            return await self.bot.send_document(chat_id=owner_id, document=bio, filename=bio.name, parse_mode=ParseMode.HTML)
                        finally:
                            try:
                                bio.close()
                            except Exception:
                                pass
                    else:
                        # Нет контента — просто значок уведомления
                        return await self.bot.send_message(chat_id=owner_id, text="ℹ️ (без контента)", parse_mode=ParseMode.HTML)

                except Exception:
                    try:
                        logger.exception("Failed to send text to owner %s", owner_id)
                    except Exception:
                        pass
                    raise


class EventHandler:
    def __init__(self, db: Any, aggregator: DeleteAggregator, config: Any, watcher_service: Any = None):
        self.db = db
        self.aggregator = aggregator
        self.config = config
        self.watcher_service = watcher_service
        self.disappearing_queue: asyncio.Queue[Tuple[int, int, Dict[str, Any]]] = asyncio.Queue()
        self._disappearing_task: Optional[asyncio.Task] = None
        self.delete_queue: asyncio.Queue[Tuple[int, events.MessageDeleted]] = asyncio.Queue()
        self._delete_task: Optional[asyncio.Task] = None

        self._muted_chat_cache: Dict[Tuple[int, int], Tuple[float, bool]] = {}
        self._muted_cache_ttl = 60.0  # seconds

    async def start_workers(self) -> None:
        await self.aggregator.start_workers()
        if not self._disappearing_task or self._disappearing_task.done():
            self._disappearing_task = asyncio.create_task(self._disappearing_worker())
        if not self._delete_task or self._delete_task.done():
            self._delete_task = asyncio.create_task(self._delete_worker())

    async def stop_workers(self) -> None:
        for task in (self._disappearing_task, self._delete_task):
            if task:
                task.cancel()
        self._disappearing_task = self._delete_task = None
        await self.aggregator.stop_workers()

    def is_chat_allowed(self, chat_id: Optional[int]) -> bool:
        if chat_id is None:
            return False
        allowed = self.config.allowed_chat_ids_set
        if allowed is None:
            return True
        return chat_id in allowed

    async def is_muted_chat(self, owner_id: int, chat_id: int) -> bool:
        key = (owner_id, chat_id)
        now_ts = time.monotonic()
        if key in self._muted_chat_cache:
            expires_at, value = self._muted_chat_cache[key]
            if now_ts < expires_at:
                return value

        # try memcached first when available
        if MEMCACHED_AVAILABLE and MC_CLIENT is not None:
            mc_key = f"muted:{owner_id}:{chat_id}"
            try:
                cached = MC_CLIENT.get(mc_key)
                if cached is not None:
                    self._muted_chat_cache[key] = (now_ts + self._muted_cache_ttl, cached)
                    return bool(cached)
            except Exception:
                pass

        row = await self.db.fetchone("SELECT 1 FROM muted_chats WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
        result = bool(row)
        self._muted_chat_cache[key] = (now_ts + self._muted_cache_ttl, result)

        if MEMCACHED_AVAILABLE and MC_CLIENT is not None:
            try:
                MC_CLIENT.set(mc_key, result, time=int(self._muted_cache_ttl))
            except Exception:
                pass

        return result

    async def _disappearing_worker(self) -> None:
        while True:
            try:
                owner_id, msg_id, payload = await self.disappearing_queue.get()
                await asyncio.sleep(2.8)
                row = await self.db.fetchone("""
                    SELECT media_path, already_forwarded, text
                    FROM pending
                    WHERE owner_id = ? AND msg_id = ?
                """, (owner_id, msg_id))
                if not row:
                    logger.debug("Disappearing record gone: %d/%d", owner_id, msg_id)
                    continue
                media_path, already_forwarded, text = row
                if already_forwarded:
                    logger.debug("Already forwarded: %d/%d", owner_id, msg_id)
                    continue
                payload.update({
                    "text": text or payload["text"],
                    "media_path": media_path if media_path and os.path.exists(media_path) else None,
                })
                await self.aggregator.add_event(owner_id, payload)
                await self.db.execute("UPDATE pending SET already_forwarded = 1 WHERE owner_id = ? AND msg_id = ?", (owner_id, msg_id))
                logger.info("Disappearing message sent after delay: %d/%d", owner_id, msg_id)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Disappearing worker error")
            finally:
                try:
                    self.disappearing_queue.task_done()
                except Exception:
                    pass

    async def handle_new(self, owner_id: int, event: events.NewMessage.Event) -> None:
        chat_id = event.chat_id
        if chat_id is None or not self.is_chat_allowed(chat_id):
            return
        if await self.db.fetchone("SELECT 1 FROM muted_chats WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)):
            return
        sender = await event.get_sender() if hasattr(event, "get_sender") else None
        if sender and (getattr(sender, "bot", False) or getattr(sender, "is_self", False)):
            return
        chat = await event.get_chat() if hasattr(event, "get_chat") else None
        text = event.raw_text or ""
        msg_id = event.id
        msg_date_iso = event.date.isoformat() if event.date else datetime.now(timezone.utc).isoformat()
        chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or "Чат"
        sender_name = get_safe_sender_name(sender)
        is_disappearing = bool(
            (getattr(event.message, "ttl_period", None) if hasattr(event, "message") else None) or
            (getattr(event.media, "ttl_seconds", None) if event.media else None)
        )
        # ==================== СТОРИС: MessageMediaStory (репост/отправка в чат) ====================
        media = getattr(event, 'media', None) or (getattr(event.message, 'media', None) if hasattr(event, 'message') else None)
        if media and isinstance(media, types.MessageMediaStory):
            story_media = media

            try:
                sender = await event.get_sender()
                sender_name = get_safe_sender_name(sender)
                sender_username = getattr(sender, 'username', None)
                sender_id = getattr(sender, 'id', None) if sender else None
            except Exception as e:
                logger.warning(f"[STORY] Ошибка получения отправителя: {e}")
                sender_name = "Unknown"
                sender_username = None
                sender_id = None

            media_path = None
            if CONFIG.download_media:
                try:
                    ext = ".jpg" if "photo" in str(story_media) else ".mp4"
                    ts = int(time.time() * 1000)
                    fname = f"story_{owner_id}_{story_media.id or ts}_{ts}{ext}"
                    path = os.path.join(self.config.media_dir, fname)

                    await asyncio.wait_for(event.download_media(file=path), timeout=20.0)

                    if os.path.exists(path) and os.path.getsize(path) > 300:
                        media_path = path
                        logger.info(f"[STORY] Скачана сторис (репост) → {path}")
                    else:
                        if os.path.exists(path):
                            os.unlink(path)
                except asyncio.TimeoutError:
                    logger.warning(f"[STORY] Таймаут скачивания репост-сторис owner={owner_id}")
                except Exception as exc:
                    logger.warning(f"[STORY] Не скачалась репост-сторис owner={owner_id}: {exc}")

            now_iso = datetime.now(timezone.utc).isoformat()
            
            try:
                # Проверяем дедупликацию по story_id
                existing = await self.db.fetchone(
                    "SELECT id FROM stories WHERE owner_id = ? AND story_id = ?",
                    (owner_id, story_media.id)
                )
                
                if not existing:
                    await self.db.execute("""
                        INSERT INTO stories 
                        (owner_id, peer_id, story_id, sender_id, sender_username, sender_name,
                         caption, media_path, posted_at, added_at, content_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        owner_id,
                        story_media.user_id,
                        story_media.id,
                        sender_id,
                        sender_username,
                        sender_name,
                        getattr(story_media, 'caption', None) or "",
                        media_path,
                        now_iso,
                        now_iso,
                        "📷 Story" if media_path and media_path.lower().endswith(('.jpg','.png')) else "🎥 Story Video"
                    ))
                    logger.info(f"[STORY] Сохранена репост-сторис {story_media.id}")
                else:
                    logger.debug(f"[STORY] Сторис {story_media.id} уже сохранена (дедуп), пропускаем")
            except Exception as e:
                logger.error(f"[STORY] Ошибка сохранения репост-сторис: {e}")

            # Отправляем владельцу
            payload = {
                "event_type": "story",
                "story_id": story_media.id,
                "text": getattr(story_media, 'caption', "") or "",
                "media_path": media_path,
                "sender_username": sender_name,
                "sender_id": sender_id,
                "chat_title": f"Сторис от {sender_name} (репост)",
                "message_date": now_iso,
                "content_type": "📖 Story",
            }
            await self.aggregator.add_event(owner_id, payload)
            logger.info(f"[STORY] Сторис {story_media.id} отправлена пользователю")
            return  # не обрабатываем дальше как обычное сообщение
        # =====================================================================================
        media_path = None
        if event.media and self.config.download_media:
            try:
                ext = detect_media_ext(event) or ".bin"
                ts = int(time.time() * 1000)
                fname = f"d_{owner_id}_{msg_id}_{ts}{ext}"
                path = os.path.join(self.config.media_dir, fname)
                if is_disappearing:
                    logger.info("Priority download disappearing media → %d/%d", owner_id, msg_id)
                    await asyncio.wait_for(event.download_media(file=path), timeout=16.0)
                else:
                    await event.download_media(file=path)
                if os.path.exists(path) and os.path.getsize(path) > 400:
                    media_path = path
                else:
                    if os.path.exists(path):
                        os.unlink(path)
                    logger.warning("Invalid/empty media file: %s", path)
            except asyncio.TimeoutError:
                logger.warning("Timeout downloading disappearing media %d/%d", owner_id, msg_id)
            except Exception as e:
                logger.warning("Media download failed %d/%d: %s", owner_id, msg_id, str(e))

        views = 0
        reactions_json = '{}'
        try:
            msg_obj = getattr(event, 'message', None) or event
            extracted_views = getattr(msg_obj, 'views', None)
            views = extracted_views if extracted_views is not None else 0
            reactions_json = extract_reactions_json(msg_obj)
            logger.debug("New message stats for owner=%s msg=%s: views=%s reactions=%s", owner_id, msg_id, views, reactions_json)
        except Exception as e:
            logger.debug("Failed to extract initial views/reactions for pending: %s", e)

        await self.db.execute("""
            INSERT OR IGNORE INTO pending (
                owner_id, chat_id, chat_title, chat_username, msg_id,
                text, original_text, edit_count, last_edited_at, media_path,
                sender_id, sender_username, message_date, added_at,
                is_disappearing, already_forwarded, views, reactions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            owner_id, chat_id, chat_title, getattr(chat, "username", None),
            msg_id, text, text, 0, None, media_path,
            getattr(sender, "id", None), sender_name,
            msg_date_iso, datetime.now(timezone.utc).isoformat(),
            1 if is_disappearing else 0, 0,
            views, reactions_json
        ))

        # Try to get richer stats from watcher in case initial event lacked reactions/views
        if self.watcher_service and chat_id is not None:
            try:
                watcher_client = self.watcher_service.watched_clients.get(owner_id)
                if watcher_client:
                    incoming_msg = await asyncio.wait_for(
                        watcher_client.get_messages(chat_id, ids=msg_id, important=True, limit=1),
                        timeout=6.0
                    )
                    if isinstance(incoming_msg, list):
                        incoming_msg = incoming_msg[0] if incoming_msg else None
                    if incoming_msg:
                        api_views = getattr(incoming_msg, 'views', None)
                        api_reactions = extract_reactions_json(incoming_msg)

                        if api_views is not None and api_views != views:
                            views = api_views
                        if api_reactions and api_reactions != '{}' and api_reactions != reactions_json:
                            reactions_json = api_reactions

                        await self.db.execute(
                            "UPDATE pending SET views=?, reactions=? WHERE owner_id=? AND msg_id=?",
                            (views, reactions_json, owner_id, msg_id)
                        )
                        logger.debug(
                            "Watcher sync for owner=%s msg=%s → views=%s reactions=%s",
                            owner_id, msg_id, views, reactions_json,
                        )
            except Exception as e:
                logger.debug("Watcher sync failed for owner=%s msg=%s: %s", owner_id, msg_id, e)

        if is_disappearing:
            payload = {
                "msg_id": msg_id,
                "text": text,
                "original_text": text,
                "edit_count": 0,
                "last_edited_at": None,
                "media_path": media_path,
                "sender_username": sender_name,
                "sender_id": getattr(sender, "id", None),
                "chat_title": chat_title,
                "chat_id": chat_id,
                "chat_username": getattr(chat, "username", None),
                "message_date": msg_date_iso,
                "content_type": detect_content_type(event),
                "event_type": "disappearing",
            }
            await self.disappearing_queue.put((owner_id, msg_id, payload))
            logger.debug("Disappearing enqueued for delayed processing: %d/%d", owner_id, msg_id)

    async def handle_edited(self, owner_id: int, event: events.MessageEdited.Event) -> None:
        chat_id = event.chat_id
        if not self.is_chat_allowed(chat_id):
            return
        if await self.is_muted_chat(owner_id, chat_id):
            return
        msg_id = event.id
        if not msg_id:
            return
        new_text = event.raw_text or ""
        edited_at_iso = (event.edit_date or datetime.now(timezone.utc)).isoformat()

        row = await self.db.fetchone("""
            SELECT id, text, original_text, edit_count, chat_title, media_path,
                   sender_id, sender_username, message_date, chat_id, chat_username
            FROM pending WHERE owner_id = ? AND msg_id = ?
        """, (owner_id, msg_id))

        if not row:
            return

        row_id, old_text, original_text, edit_count, chat_title, media_path, \
        sender_id, sender_username, message_date, chat_id, chat_username = row

        old_text = old_text or ""
        if original_text is None:
            original_text = old_text

        if new_text == old_text:
            return

        new_edit_count = int(edit_count or 0) + 1

        msg_obj = getattr(event, 'message', None) or event
        current_views = getattr(msg_obj, 'views', None)
        current_reactions = extract_reactions_json(msg_obj)

        # Keep existing rows if we can't parse current values.
        row_views, row_reactions = await self.db.fetchone(
            "SELECT views, reactions FROM pending WHERE id = ?", (row_id,)
        ) or (None, None)

        if current_views is None:
            current_views = int(row_views or 0)
        if not current_reactions or current_reactions == '{}':
            current_reactions = row_reactions or '{}'

        await self.db.execute("""
            UPDATE pending
            SET text = ?, original_text = ?, edit_count = ?, last_edited_at = ?, views = ?, reactions = ?
            WHERE id = ?
        """, (
            new_text,
            original_text,
            new_edit_count,
            edited_at_iso,
            current_views,
            current_reactions,
            row_id
        ))

        if _is_minor_edit(original_text, new_text):
            return

        content_type = guess_content_type_from_path(media_path)

        payload = {
            "msg_id": msg_id,
            "text": new_text,
            "original_text": original_text,
            "edit_count": new_edit_count,
            "last_edited_at": edited_at_iso,
            "media_path": media_path,
            "sender_username": sender_username,
            "sender_id": sender_id,
            "chat_title": chat_title,
            "chat_id": chat_id,
            "message_date": message_date,
            "edited_at": edited_at_iso,
            "content_type": content_type,
            "event_type": "edited",
        }

        try:
            log_owner_event(owner_id, event_kind="message_edited", data={
                "message_id": msg_id, "chat_id": chat_id, "chat_title": chat_title,
                "sender_id": sender_id, "sender_username": sender_username,
                "content_type": content_type, "before_text": original_text,
                "after_text": new_text, "edit_count": new_edit_count,
                "edited_at": edited_at_iso, "message_date": message_date,
                "media_path": media_path,
            })
        except Exception:
            logger.exception("Failed to log edited event")

        await self.aggregator.add_event(owner_id, payload)

    async def handle_deleted(self, owner_id: int, event: events.MessageDeleted.Event) -> None:
        await self.delete_queue.put((owner_id, event))

    async def _delete_worker(self) -> None:
        while True:
            try:
                owner_id, event = await self.delete_queue.get()
                await self._process_delete(owner_id, event)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Delete worker crashed")
            finally:
                try:
                    self.delete_queue.task_done()
                except Exception:
                    pass

    async def _process_delete(self, owner_id: int, event: events.MessageDeleted.Event) -> None:
        """
        Надёжное перемещение удалённых сообщений из pending в deleted_messages.
        - Обработка ошибок на каждом этапе
        - Дедупликация (не вставляем дубликаты)
        - Atomicity: либо полностью успех, либо откат
        - Полное логирование каждого действия
        """
        event_chat_id = getattr(event, "chat_id", None)
        
        # Проверка разрешённых чатов
        if event_chat_id is not None and not self.is_chat_allowed(event_chat_id):
            return
            
        deleted_ids = set(event.deleted_ids or [])
        if not deleted_ids:
            return

        # Попытка получить реальный chat_id из события (важно для каналов)
        final_chat_id = event_chat_id
        try:
            if hasattr(event, "get_chat"):
                chat = await event.get_chat()
                final_chat_id = getattr(chat, "id", None) or final_chat_id
        except Exception as e:
            logger.debug("Failed to get_chat in _process_delete for owner=%s: %s", owner_id, e)

        # Финальная проверка muted chats (используем finalized chat_id)
        if final_chat_id and await self.is_muted_chat(owner_id, final_chat_id):
            logger.debug("Chat %s is muted for owner %s, skipping delete processing", final_chat_id, owner_id)
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        processed_count = 0
        skipped_ids = []
        error_ids = []

        for msg_id in deleted_ids:
            try:
                # === STEP 1: Найти запись в pending ===
                row = None
                for attempt in range(1, 26):
                    try:
                        row = await self.db.fetchone("""
                            SELECT id, chat_title, text, original_text, edit_count, last_edited_at,
                                   media_path, sender_id, sender_username, message_date, chat_id,
                                   chat_username, COALESCE(already_forwarded, 0),
                                   COALESCE(views, 0), COALESCE(reactions, '{}')
                            FROM pending WHERE owner_id = ? AND msg_id = ?
                        """, (owner_id, msg_id))
                        if row:
                            break
                    except Exception as e:
                        logger.debug("Attempt %d to fetch pending row failed: %s", attempt, e)
                    if attempt < 25:
                        await asyncio.sleep(0.07)

                if not row:
                    logger.debug("Message %d not found in pending for owner %s (normal for external deletes)", 
                                msg_id, owner_id)
                    skipped_ids.append(msg_id)
                    continue

                (row_id, chat_title, text, original_text, edit_count, last_edited_at,
                 media_path, sender_id, sender_username, message_date, db_chat_id,
                 chat_username, already_forwarded, row_views, row_reactions) = row

                logger.debug("delete_fetch owner=%s msg=%s row_views=%s row_reactions=%s", owner_id, msg_id, row_views, row_reactions)

                # Нормализуем текст
                text = text or ""
                original_text = original_text or text
                edit_count = int(edit_count or 0)
                row_views = int(row_views or 0)
                row_reactions = row_reactions or '{}'                
                # Используем chat_id из базы (наиболее надёжный источник для этого сообщения)
                final_msg_chat_id = db_chat_id or final_chat_id or event_chat_id

                # === STEP 2: Проверить дедупликацию ===
                existing = await self.db.fetchone(
                    "SELECT id FROM deleted_messages WHERE owner_id=? AND msg_id=?",
                    (owner_id, msg_id)
                )
                if existing:
                    logger.debug("Message %d already in deleted_messages for owner %s, cleaning up pending",
                                owner_id, msg_id)
                    try:
                        await self.db.execute("DELETE FROM pending WHERE id=?", (row_id,))
                    except Exception as e:
                        logger.warning("Failed to delete pending row %d after dedup check: %s", row_id, e)
                    skipped_ids.append(msg_id)
                    continue

                # === STEP 3: Определить тип контента ===
                content_type = guess_content_type_from_path(media_path)

                # === STEP 3.5: Получить views и reactions ===
                views = row_views if 'row_views' in locals() else 0
                reactions_json = row_reactions if 'row_reactions' in locals() else '{}'
                try:
                    # Пробуем получить последнее состояние сообщения из Telegram
                    if self.watcher_service:
                        client = self.watcher_service.watched_clients.get(owner_id)
                        if client:
                            try:
                                messages = await asyncio.wait_for(
                                    client.get_messages(final_msg_chat_id, ids=msg_id, important=True, limit=1),
                                    timeout=8.0
                                )
                                # get_messages может вернуть Message, список Message, или None
                                msg = None
                                if isinstance(messages, list):
                                    msg = messages[0] if messages else None
                                elif messages is not None:
                                    msg = messages

                                if msg:
                                    tmp_views = getattr(msg, 'views', None)
                                    if tmp_views is not None:
                                        views = tmp_views or 0

                                    tmp_reactions = extract_reactions_json(msg)
                                    if tmp_reactions and tmp_reactions != '{}':
                                        reactions_json = tmp_reactions

                                    # Если не удалось получить реакции, пробуем вызвать to_dict
                                    if reactions_json == '{}':
                                        try:
                                            msg_dict = msg.to_dict() if hasattr(msg, 'to_dict') else {}
                                            if isinstance(msg_dict, dict):
                                                raw_reactions = msg_dict.get('reactions') or msg_dict.get('reactions_data')
                                                if raw_reactions:
                                                    tmp_reactions2 = extract_reactions_json(raw_reactions)
                                                    if tmp_reactions2 and tmp_reactions2 != '{}':
                                                        reactions_json = tmp_reactions2
                                                        logger.debug("Fallback to msg_dict reactions for msg %d: %s", msg_id, reactions_json)
                                        except Exception:
                                            pass

                                    logger.debug("Retrieved msg views=%s, reactions=%s for msg %d (fallback row views=%s, reactions=%s)",
                                                 views, reactions_json, msg_id, row_views, row_reactions)
                            except asyncio.TimeoutError:
                                logger.debug("Timeout getting message state for views/reactions: %d/%d", owner_id, msg_id)
                            except Exception as e:
                                logger.debug("Failed to get message state for views/reactions: %s", e)
                except Exception as e:
                    logger.debug("Error in views/reactions extraction block: %s", e)

                # === STEP 4: INSERT в deleted_messages (атомарно) ===
                try:
                    await self.db.execute("""
                        INSERT INTO deleted_messages (
                            owner_id, chat_id, chat_title, chat_username, msg_id,
                            sender_id, sender_username, content_type, text_preview,
                            original_text_preview, edit_count, last_edited_at, media_path,
                            original_timestamp, saved_at, views, reactions
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        owner_id, final_msg_chat_id, chat_title, chat_username, msg_id,
                        sender_id, sender_username, content_type,
                        text[:50] if text else "", 
                        original_text[:50] if original_text else "", 
                        edit_count, last_edited_at,
                        media_path, message_date, now_iso, views, reactions_json
                    ))
                    logger.debug("Inserted deleted_message %d/%d into DB", owner_id, msg_id)
                except Exception as e:
                    logger.error("CRITICAL: Failed to INSERT deleted_message %d for owner %s: %s",
                                msg_id, owner_id, e)
                    error_ids.append(msg_id)
                    continue  # Не удаляем из pending, чтобы не потерять

                # === STEP 5: DELETE из pending (атомарно) ===
                try:
                    await self.db.execute("DELETE FROM pending WHERE id=?", (row_id,))
                    logger.debug("Deleted pending row %d for message %d", row_id, msg_id)
                except Exception as e:
                    logger.error("CRITICAL: Failed to DELETE pending row %d (msg %d): %s",
                                row_id, msg_id, e)
                    # Запись уже в deleted_messages, но осталась в pending
                    # Это не критично, но логируем для отслеживания

                # === STEP 6: Логирование события ===
                try:
                    log_owner_event(owner_id, event_kind="message_deleted", data={
                        "message_id": msg_id, 
                        "chat_id": final_msg_chat_id, 
                        "chat_title": chat_title,
                        "sender_id": sender_id, 
                        "sender_username": sender_username,
                        "content_type": content_type, 
                        "text": text, 
                        "original_text": original_text,
                        "edit_count": edit_count, 
                        "last_edited_at": last_edited_at,
                        "media_path": media_path, 
                        "message_date": message_date,
                        "deleted_at": now_iso,
                    })
                except Exception as e:
                    logger.debug("Failed to log delete event for %d: %s", msg_id, e)

                # === STEP 7: Отправить владельцу (если не отправляли ранее) ===
                if not already_forwarded:
                    payload = {
                        "msg_id": msg_id,
                        "text": text,
                        "original_text": original_text,
                        "edit_count": edit_count,
                        "last_edited_at": last_edited_at,
                        "media_path": media_path,
                        "sender_username": sender_username,
                        "sender_id": sender_id,
                        "chat_title": chat_title,
                        "chat_id": final_msg_chat_id,
                        "message_date": message_date,
                        "deleted_at": now_iso,
                        "content_type": content_type,
                        "event_type": "deleted",
                        "views": views,
                        "reactions": reactions_json,
                    }
                    try:
                        await self.aggregator.add_event(owner_id, payload)
                        logger.debug("Queued delete event for forwarding: %d/%d", owner_id, msg_id)
                    except Exception as e:
                        logger.error("Failed to queue delete event for owner %s msg %d: %s",
                                    owner_id, msg_id, e)

                # === STEP 8: Очистить старые записи ===
                try:
                    await self.db.clean_old_records(owner_id)
                except Exception as e:
                    logger.debug("Failed to clean old records for owner %s: %s", owner_id, e)

                processed_count += 1

            except Exception as e:
                logger.exception("Unhandled error processing delete msg_id=%d owner=%s",
                                msg_id, owner_id)
                error_ids.append(msg_id)

        # === Финальное логирование ===
        if processed_count > 0 or skipped_ids or error_ids:
            logger.info(
                "Delete batch processed: owner=%s processed=%d skipped=%d errors=%d (ids=%s)",
                owner_id, processed_count, len(skipped_ids), len(error_ids),
                error_ids[:5] if error_ids else "none"
            )


async def check_user_allowed(db: Database, user_id: int) -> bool:
    row = await db.fetchone(
        "SELECT status FROM access_requests WHERE user_id=?",
        (user_id,)
    )

    if not row:
        return False

    return bool(row[0])

async def is_user_fully_approved(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if user_id in CONFIG.admin_ids:
        return True

    app = context.application.bot_data.get("app")
    if not app:
        return False

    row = await app.db.fetchone(
        "SELECT approved FROM users WHERE user_id = ?",
        (user_id,)
    )

    return bool(row and row[0] == 1)

async def send_admin_approval_request(db: Database, bot, user_id: int, username: Optional[str]):

    now = datetime.now(timezone.utc).isoformat()

    try:
        await db.execute(
            """
            UPDATE bot_users
            SET requested_at=?
            WHERE user_id=?
            """,
            (now, user_id)
        )
    except Exception as e:
        logger.exception("Failed to update bot_users.requested_at for %s: %s", user_id, e)

    try:
        await db.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, username, first_name, last_name, first_seen_at, requested_at, approved)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (user_id, username or None, None, None, now, now)
        )
    except Exception as e:
        logger.exception("Failed to insert/update users for %s: %s", user_id, e)

    if not CONFIG.admin_ids:
        return

    text = (
        f"👤 Новый пользователь хочет доступ\n\n"
        f"ID: {user_id}\n"
        f"Username: {username}\n\n"
        f"Разрешить доступ?"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Разрешить", callback_data=f"approve_user:{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"deny_user:{user_id}")
    ]])

    for admin in CONFIG.admin_ids:
        try:
            await bot.send_message(
                admin,
                text,
                reply_markup=keyboard
            )
        except Exception:
            logger.exception("Failed notify admin")
