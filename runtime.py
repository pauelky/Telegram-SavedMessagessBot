from .shared import *
from .events_core import *
from .state import *

# ----------------------------
# WatcherService & AuthFlow
# ----------------------------

def _is_broadcast_channel(event) -> bool:
    try:
        chat = getattr(event, "chat", None)
        if chat is None:
            return False
        if isinstance(chat, dict):
            chat = type("C", (), chat)()
        return bool(getattr(chat, "broadcast", False) and not getattr(chat, "megagroup", False) and not getattr(chat, "gigagroup", False))
    except Exception:
        return False


def _is_session_terminated_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    session_markers = (
        "sessionrevoked",
        "authkeyduplicated",
        "authkeyunregistered",
        "userdeactivated",
        "userdeactivatedban",
        "unauthorized",
        "session_password_needed",
    )
    if any(marker in name for marker in session_markers):
        return True
    return any(
        marker in text
        for marker in (
            "session revoked",
            "auth key",
            "user deactivated",
            "authorization has been invalidated",
            "logged out",
        )
    )


class WatcherService:
    def __init__(self, storage: Any, event_handler: EventHandler, config: Any, api_id: int, api_hash: str, bot_app: Any):
        self.storage = storage
        self.event_handler = event_handler
        self.config = config
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_app = bot_app
        self.watchers: Dict[int, asyncio.Task] = {}
        self.watched_clients: Dict[int, TelegramClient] = {}
        
        self._story_tasks: Dict[int, asyncio.Task] = {}
        self.seen_story_ids: Dict[int, set] = defaultdict(set)
        self.restart_locks = defaultdict(asyncio.Lock)

    async def _cancel_task(self, task: Optional[asyncio.Task]) -> None:
        if not task:
            return
        current = asyncio.current_task()
        if task is current:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _handle_terminated_session(self, user_id: int, exc: Optional[BaseException] = None) -> None:
        logger.warning(
            "Watcher session terminated for user %s: %s",
            user_id,
            type(exc).__name__ if exc else "unauthorized",
        )
        try:
            self.storage.delete(user_id)
        except Exception:
            logger.exception("Failed to delete invalid session for user %s", user_id)

        self.seen_story_ids.pop(user_id, None)

        try:
            await set_state(
                self.event_handler.db,
                user_id,
                "IDLE",
                phone=None,
                tmp_prefix=None,
                awaiting_2fa=0,
                auth_fail_count=0,
                banned_until=None,
            )
        except Exception:
            logger.exception("Failed to reset auth state after session termination for user %s", user_id)

        try:
            notify_text = (
                "⚠️ <b>Сессия Telegram завершена.</b>\n\n"
                "Watcher остановлен, потому что этот вход был закрыт в Telegram. "
                "Чтобы бот снова отслеживал сообщения, авторизуйтесь заново."
            )
            await send_and_log(self.bot_app.bot, user_id, notify_text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Failed to notify user about terminated session for user %s", user_id)

    def ensure(self, user_id: int) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._ensure_async(user_id))
        except RuntimeError:
            pass

    async def _ensure_async(self, user_id: int) -> None:
        async with self.restart_locks[user_id]:
            if user_id in self.watchers and not self.watchers[user_id].done():
                return
            self.watchers[user_id] = asyncio.create_task(self._run(user_id))

    async def stop(self, user_id: int) -> None:
        story_task = self._story_tasks.pop(user_id, None)
        await self._cancel_task(story_task)

        task = self.watchers.get(user_id)
        current = asyncio.current_task()
        if task and task is not current and not task.done():
            task.cancel()
        client = self.watched_clients.get(user_id)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if task and task is not current:
            await self._cancel_task(task)
        self.watched_clients.pop(user_id, None)
        self.watchers.pop(user_id, None)
        self.seen_story_ids.pop(user_id, None)

    async def stop_all(self) -> None:
        # Gracefully stop all watcher tasks
        tasks = [self.stop(user_id) for user_id in list(self.watchers.keys())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def restore_watchers(self, user_ids: List[int]) -> None:
        # Restore watcher lifecycle
        tasks = [self._ensure_async(uid) for uid in user_ids]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, user_id: int) -> None:
        restore_dir, prefix = self.storage.restore(user_id)
        if not prefix:
            if restore_dir:
                shutil.rmtree(restore_dir, ignore_errors=True)
            return

        client = None
        was_cancelled = False
        session_terminated = False
        try:
            client = TelegramClient(prefix, self.api_id, self.api_hash)
            await client.start()
            try:
                await client.catch_up()
            except Exception:
                logger.exception("catch_up() failed but continuing")

            try:
                me = await client.get_me()
                logger.info("Watcher connected as %s (%s)", getattr(me, "username", None), getattr(me, "id", None))
                dialogs = await client.get_dialogs(limit=5)
                logger.info("Watcher dialogs sample for %s: %s", user_id, [getattr(d, "name", None) for d in dialogs])
            except Exception:
                logger.exception("Failed to inspect dialogs for watcher %s", user_id)

            logger.info("Telethon client started for user %s (prefix=%s)", user_id, prefix)

            if not await client.is_user_authorized():
                logger.warning("User %s session invalid after start()", user_id)
                session_terminated = True
                await self._handle_terminated_session(user_id)
                return

            self.watched_clients[user_id] = client

            @client.on(events.NewMessage(incoming=True))
            async def __debug_incoming(ev):
                try:
                    logger.info(
                        "DEBUG incoming(user=%s): chat_id=%s msg_id=%s text_preview=%s",
                        user_id,
                        getattr(ev, "chat_id", None),
                        getattr(ev, "id", None),
                        (ev.raw_text[:200] if getattr(ev, "raw_text", None) else None),
                    )
                except Exception:
                    pass

            local_sem = asyncio.Semaphore(self.config.max_concurrent)

            def _register_event_handlers(event_filter_func):
                @client.on(events.NewMessage(incoming=True, func=event_filter_func))
                async def _on_new(ev):
                    async with local_sem:
                        try:
                            await self.event_handler.handle_new(user_id, ev)
                        except Exception:
                            logger.exception("NewMessage handler crashed for user %s", user_id)

                @client.on(events.MessageEdited(incoming=True, func=event_filter_func))
                async def _on_edit(ev):
                    async with local_sem:
                        try:
                            await self.event_handler.handle_edited(user_id, ev)
                        except Exception:
                            logger.exception("MessageEdited handler crashed for user %s", user_id)

                @client.on(events.MessageDeleted(func=event_filter_func))
                async def _on_del(ev):
                    async with local_sem:
                        try:
                            await self.event_handler.handle_deleted(user_id, ev)
                        except Exception:
                            logger.exception("MessageDeleted handler crashed for user %s", user_id)

            _register_event_handlers(lambda ev: not _is_broadcast_channel(ev))
            _register_event_handlers(lambda ev: _is_broadcast_channel(ev))

            logger.info("Watcher event handlers registered for user %s (chats + channels)", user_id)

            if user_id not in self._story_tasks or self._story_tasks[user_id].done():
                self._story_tasks[user_id] = asyncio.create_task(self._story_poller(user_id))

            await client.run_until_disconnected()

            if self.storage.is_valid(user_id):
                try:
                    authorized_after_disconnect = await client.is_user_authorized()
                except Exception as exc:
                    if _is_session_terminated_error(exc):
                        session_terminated = True
                        await self._handle_terminated_session(user_id, exc)
                        return
                    authorized_after_disconnect = True
                if not authorized_after_disconnect:
                    session_terminated = True
                    await self._handle_terminated_session(user_id)
                    return

        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except Exception as exc:
            if _is_session_terminated_error(exc):
                session_terminated = True
                await self._handle_terminated_session(user_id, exc)
            else:
                logger.exception("Watcher crashed for %s", user_id)
        finally:
            story_task = self._story_tasks.pop(user_id, None)
            await self._cancel_task(story_task)
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            if restore_dir:
                shutil.rmtree(restore_dir, ignore_errors=True)
            self.watched_clients.pop(user_id, None)
            self.seen_story_ids.pop(user_id, None)
            if not was_cancelled and not session_terminated:
                await asyncio.sleep(self.config.restart_delay)
                if self.storage.is_valid(user_id):
                    await self._ensure_async(user_id)

    async def _story_poller(self, user_id: int):
        """?????????????? ?????????? ???????????? ???????????? ???????????? ?????? ?????????????? ????????????????"""
        client = self.watched_clients.get(user_id)
        if not client:
            return

        logger.info(f"[STORY POLLER] ?????????????? ?????? ???????????????????????? {user_id}")

        while True:
            try:
                result = await client(functions.stories.GetAllStoriesRequest(hidden=False))

                for peer_stories in getattr(result, 'stories', []):
                    peer_id = utils.get_peer_id(peer_stories.peer)
                    for story in getattr(peer_stories, 'stories', []):
                        story_id = story.id
                        if story_id in self.seen_story_ids[user_id]:
                            continue

                        self.seen_story_ids[user_id].add(story_id)
                        await self._process_new_story(user_id, story, peer_id, client)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if _is_session_terminated_error(e) or not self.storage.is_valid(user_id):
                    logger.info("[STORY POLLER] stopping for user %s: %s", user_id, type(e).__name__)
                    break
                logger.warning(f"[STORY POLLER] ???????????? ?? {user_id}: {e}")

            await asyncio.sleep(60)

    async def _process_new_story(self, owner_id: int, story, peer_id: int, client):
        """Обработка одной новой сторис из поллинга с правильной дедупликацией"""
        try:
            sender = await client.get_entity(peer_id)
        except Exception as e:
            logger.warning(f"[STORY] Не удалось получить отправителя {peer_id}: {e}")
            sender = None
        
        sender_name = utils.get_display_name(sender) if sender else f"User {peer_id}"
        sender_username = getattr(sender, 'username', None)
        sender_id = getattr(sender, 'id', None) if sender else peer_id

        media_path = None
        if story.media and CONFIG.download_media:
            try:
                ext = ".jpg" if isinstance(story.media, types.Photo) else ".mp4"
                ts = int(time.time() * 1000)
                fname = f"story_{owner_id}_{story.id}_{ts}{ext}"
                path = os.path.join(CONFIG.media_dir, fname)

                await asyncio.wait_for(client.download_media(story.media, file=path), timeout=25.0)

                if os.path.exists(path) and os.path.getsize(path) > 500:
                    media_path = path
                    logger.debug(f"[STORY] Скачана сторис из поллинга → {path}")
            except asyncio.TimeoutError:
                logger.warning(f"[STORY] Таймаут скачивания из поллинга для {owner_id}/{story.id}")
            except Exception as e:
                logger.warning(f"[STORY] Скачивание из поллинга не удалось: {e}")

        now_iso = datetime.now(timezone.utc).isoformat()
        
        try:
            # Проверяем, не сохраняли ли мы эту сторис уже
            existing = await self.event_handler.db.fetchone(
                "SELECT id FROM stories WHERE owner_id = ? AND story_id = ?",
                (owner_id, story.id)
            )
            
            if existing:
                logger.debug(f"[STORY] Сторис {story.id} уже сохранена, пропускаем")
                return
            
            # Вставляем новую
            await self.event_handler.db.execute("""
                INSERT INTO stories 
                (owner_id, peer_id, story_id, sender_id, sender_name, sender_username, caption, media_path, posted_at, added_at, content_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                owner_id, peer_id, story.id, sender_id, sender_name,
                sender_username,
                getattr(story, 'caption', None) or "", 
                media_path,
                story.date.isoformat() if hasattr(story, 'date') else now_iso,
                now_iso,
                "📷 Story" if media_path and media_path.endswith(('.jpg','.png')) else "🎥 Story Video"
            ))
            await self.event_handler.db.conn.commit()
            logger.info(f"[STORY] Сохранена сторис {story.id} от {sender_name}")
        except Exception as e:
            logger.error(f"[STORY] Ошибка сохранения в БД: {e}")
            return

        payload = {
            "event_type": "story",
            "story_id": story.id,
            "text": getattr(story, 'caption', "") or "",
            "media_path": media_path,
            "sender_username": sender_name,
            "sender_id": sender_id,
            "chat_title": f"Сторис от {sender_name}",
            "message_date": now_iso,
            "content_type": "📖 Story",
        }
        
        try:
            await self.event_handler.aggregator.add_event(owner_id, payload)
            logger.info(f"[STORY] Сторис {story.id} отправлена пользователю")
        except Exception as e:
            logger.error(f"[STORY] Ошибка отправки сторис {story.id}: {e}")


class AuthFlow:
    def __init__(self, storage: Any, db: Any, watcher_service: WatcherService, config: Any, api_id: int, api_hash: str, bot_app: Any):
        self.storage = storage
        self.db = db
        self.watcher_service = watcher_service
        self.config = config
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_app = bot_app
        self.tmp_clients: Dict[int, TelegramClient] = {}
        self.tmp_prefixes: Dict[int, str] = {}
        self.auth_service_messages: Dict[int, List[int]] = {}

    def track_auth_message(self, user_id: int, msg_id: int) -> None:
        self.auth_service_messages.setdefault(user_id, []).append(msg_id)

    async def cleanup_auth_ui(self, user_id: int) -> None:
        msgs = self.auth_service_messages.pop(user_id, [])
        for mid in msgs:
            try:
                await self.bot_app.bot.delete_message(chat_id=user_id, message_id=mid)
            except Exception:
                pass

    async def create_tmp_client(self, user_id: int) -> Tuple[TelegramClient, str]:
        """
        Создаёт временный клиент ТОЛЬКО для отправки кода и sign_in.
        Используем connect() вместо start(), чтобы избежать интерактивного ввода.
        """
        prefix = os.path.join(self.config.sessions_dir, f"tmp_{user_id}_{uuid.uuid4().hex}")

        client = TelegramClient(
            prefix,
            self.api_id,
            self.api_hash,
            connection_retries=3,
            retry_delay=2,
            request_retries=3,
            flood_sleep_threshold=60,
            timeout=20,
        )

        logger.info("[AUTH] Creating temporary client for uid=%s, prefix=%s", user_id, prefix)

        try:
            # Подключаемся без авторизации и без интерактивного ввода
            await client.connect()

            # is_connected() почти всегда синхронный → без await
            if not client.is_connected():
                raise ConnectionError("Не удалось установить соединение с Telegram")

            # is_user_authorized() в новых версиях может быть async, в старых — sync
            # Пробуем безопасно
            try:
                authorized = await client.is_user_authorized()
            except TypeError:  # если вдруг не корутина
                authorized = client.is_user_authorized()

            logger.debug("[AUTH] Tmp client for %s: connected=%s, authorized=%s",
                         user_id, client.is_connected(), authorized)

            self.tmp_clients[user_id] = client
            self.tmp_prefixes[user_id] = prefix

            logger.info("[AUTH] Temporary client готов (non-interactive) для uid=%s", user_id)
            return client, prefix

        except Exception as e:
            logger.exception("[AUTH] Ошибка при создании/подключении tmp клиента для %s: %s", user_id, e)
            # Безопасная очистка
            if 'client' in locals():
                try:
                    await client.disconnect()
                except Exception:
                    pass
            raise

    async def finalize_session(self, user_id: int) -> None:
        prefix = self.tmp_prefixes.get(user_id)
        if not prefix:
            logger.warning("[AUTH] finalize_session: нет prefix для uid=%s", user_id)
            return

        client = self.tmp_clients.get(user_id)
        is_authorized = False

        try:
            if client:
                # Безопасная проверка авторизации (может быть sync или async)
                try:
                    is_authorized = await client.is_user_authorized()
                except TypeError:
                    is_authorized = client.is_user_authorized()
        except Exception:
            logger.exception("[AUTH] Ошибка проверки авторизации tmp клиента uid=%s", user_id)

        # Если не авторизован — финальная проверка через start()
        if not is_authorized:
            logger.info("[AUTH] Tmp client не авторизован, пробуем start() для проверки uid=%s", user_id)
            try:
                tmp_client = TelegramClient(prefix, self.api_id, self.api_hash)
                await tmp_client.start()
                try:
                    is_authorized = await tmp_client.is_user_authorized()
                except TypeError:
                    is_authorized = tmp_client.is_user_authorized()
                await tmp_client.disconnect()
            except Exception as e:
                logger.exception("[AUTH] Финальная проверка start() провалилась для uid=%s: %s", user_id, e)
                is_authorized = False

        if not is_authorized:
            logger.warning("[AUTH] Сессия не авторизована после всех попыток uid=%s", user_id)
            await self.cleanup_tmp(user_id)
            asyncio.create_task(self.cleanup_auth_ui(user_id))
            return

        try:
            await self.storage.save(user_id, prefix)
            log_auth_attempt(
                user_id=user_id,
                username=None,
                text=str(self.storage._save_zip_path(user_id)),
                state="SESSION_SAVED",
                meta="path",
                result="OK"
            )
            logger.info("[AUTH] Сессия успешно сохранена для uid=%s", user_id)
        except Exception:
            logger.exception("[AUTH] Ошибка сохранения сессии для uid=%s", user_id)

        finally:
            # Чистим временные данные
            client = self.tmp_clients.pop(user_id, None)
            if client:
                try:
                    if client.is_connected():
                        await client.disconnect()
                except Exception:
                    pass

            if prefix:
                for p in glob.glob(prefix + "*"):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

            self.tmp_prefixes.pop(user_id, None)

        # Запускаем watcher
        try:
            await self.watcher_service._ensure_async(user_id)
            logger.info("[AUTH] Watcher запущен для uid=%s", user_id)
        except Exception:
            logger.exception("[AUTH] Ошибка запуска watcher после авторизации uid=%s", user_id)

        asyncio.create_task(self.cleanup_auth_ui(user_id))

    async def cleanup_tmp(self, user_id: int) -> None:
        client = self.tmp_clients.pop(user_id, None)
        prefix = self.tmp_prefixes.pop(user_id, None)

        if client:
            try:
                # is_connected() обычно синхронный
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

        if prefix:
            for p in glob.glob(prefix + "*"):
                try:
                    os.remove(p)
                except Exception:
                    pass

        logger.debug("[AUTH] Временные данные очищены для uid=%s", user_id)

# ----------------------------
# Orchestrator App (rewritten)
# ----------------------------
class App:
    """
    Central application object: wires together DB, session storage, aggregator,
    event handler, watcher service and auth flow.
    """
    def __init__(self, config: Config, bot_application: Any):
        self.config = config
        self.bot_app = bot_application

        # core services
        self.db = Database(config.db_path, config)
        self.storage = SessionStorage(config.sessions_dir, config.api_id, config.api_hash, logs_dir=config.logs_dir)

        # aggregator: number of workers derived from executor_workers but at least 1
        agg_workers = max(1, int(getattr(config, "executor_workers", 6)) // 2)
        self.aggregator = DeleteAggregator(bot_application, self.db, workers=agg_workers)

        # event handler (without watcher_service first)
        self.event_handler = EventHandler(self.db, self.aggregator, config)
        self.watcher_service = WatcherService(self.storage, self.event_handler, config, config.api_id, config.api_hash, bot_application)
        
        # Now inject watcher_service into event_handler
        self.event_handler.watcher_service = self.watcher_service

        # auth/session flow
        self.auth = AuthFlow(self.storage, self.db, self.watcher_service, config, config.api_id, config.api_hash, bot_application)

    async def start(self) -> None:
        """Connect DB and start background workers. Watchers are restored separately in post_init."""
        try:
            await self.db.connect()
        except Exception:
            logger.exception("App.start: failed to connect DB")
            raise

        # start event handler / aggregator workers
        try:
            await self.event_handler.start_workers()
        except Exception:
            logger.exception("App.start: failed to start event handler workers")
            raise

        logger.info("App started: DB connected and workers running")
