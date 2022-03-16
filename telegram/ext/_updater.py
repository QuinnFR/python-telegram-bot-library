#!/usr/bin/env python
#
# A library that provides a Python interface to the Telegram Bot API
# Copyright (C) 2015-2022
# Leandro Toledo de Souza <devs@python-telegram-bot.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser Public License for more details.
#
# You should have received a copy of the GNU Lesser Public License
# along with this program.  If not, see [http://www.gnu.org/licenses/].
"""This module contains the class Updater, which tries to make creating Telegram bots intuitive."""
import asyncio
import logging
import ssl
from pathlib import Path
from types import TracebackType
from typing import (
    Callable,
    List,
    Optional,
    Union,
    TypeVar,
    TYPE_CHECKING,
    Coroutine,
    Type,
)

from telegram._utils.defaultvalue import DEFAULT_NONE
from telegram._utils.types import ODVInput
from telegram.error import InvalidToken, RetryAfter, TimedOut, TelegramError
from telegram.ext._utils.webhookhandler import WebhookAppClass, WebhookServer

if TYPE_CHECKING:
    from telegram import Bot


_UpdaterType = TypeVar('_UpdaterType', bound="Updater")


class Updater:
    """This class fetches updates for the bot either via long polling or by starting a webhook
    server. Received updates are enqueued into the :attr:`update_queue` and may be fetched from
    there to handle them appropriately.

    .. versionchanged:: 14.0

        * Removed argument and attribute ``user_sig_handler``
        * The only arguments and attributes are now :attr:`bot` and :attr:`update_queue` as now
          the sole purpose of this class is to fetch updates. The entry point to a PTB application
          is now :class:`telegram.ext.Application`.

    Args:
        bot (:class:`telegram.Bot`): The bot used with this Updater.
        update_queue (:class:`asyncio.Queue`): Queue for the updates.

    Attributes:
        bot (:class:`telegram.Bot`): The bot used with this Updater.
        update_queue (:class:`asyncio.Queue`): Queue for the updates.

    """

    __slots__ = (
        'bot',
        '_logger',
        'update_queue',
        'last_update_id',
        '_running',
        '_initialized',
        '_httpd',
        '__lock',
        '__polling_task',
    )

    def __init__(
        self,
        bot: 'Bot',
        update_queue: asyncio.Queue,
    ):
        self.bot = bot
        self.update_queue = update_queue

        self.last_update_id = 0
        self._running = False
        self._initialized = False
        self._httpd: Optional[WebhookServer] = None
        self.__lock = asyncio.Lock()
        self.__polling_task: Optional[asyncio.Task] = None
        self._logger = logging.getLogger(__name__)

    @property
    def running(self) -> bool:
        return self._running

    async def initialize(self) -> None:
        """Initialize the Updater & the associated :attr:`bot` by calling
        :meth:`telegram.Bot.initialize`.

        .. seealso::
            :meth:`shutdown`
        """
        if self._initialized:
            self._logger.debug('This Updater is already initialized.')
            return

        await self.bot.initialize()
        self._initialized = True

    async def shutdown(self) -> None:
        """
        Shutdown the Updater & the associated :attr:`bot` by calling :meth:`telegram.Bot.shutdown`.

        .. seealso::
            :meth:`initialize`

        Raises:
            :exc:`RuntimeError`: If the updater is still running.
        """
        if self.running:
            raise RuntimeError('This Updater is still running!')

        if not self._initialized:
            self._logger.warning('This Updater is already shut down.')
            return

        await self.bot.shutdown()
        self._initialized = False
        self._logger.debug('Shut down of Updater complete')

    async def __aenter__(self: _UpdaterType) -> _UpdaterType:
        """Simple context manager which initializes the Updater."""
        try:
            await self.initialize()
            return self
        except Exception as exc:
            await self.shutdown()
            raise exc

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        """Shutdown the Updater from the context manager."""
        # Make sure not to return `True` so that exceptions are not suppressed
        # https://docs.python.org/3/reference/datamodel.html?#object.__aexit__
        await self.shutdown()

    async def start_polling(
        self,
        poll_interval: float = 0.0,
        timeout: int = 10,
        bootstrap_retries: int = -1,
        read_timeout: float = 2,
        write_timeout: ODVInput[float] = DEFAULT_NONE,
        connect_timeout: ODVInput[float] = DEFAULT_NONE,
        pool_timeout: ODVInput[float] = DEFAULT_NONE,
        allowed_updates: List[str] = None,
        drop_pending_updates: bool = None,
        error_callback: Callable[[TelegramError], None] = None,
    ) -> asyncio.Queue:
        """Starts polling updates from Telegram.

        .. versionchanged:: 14.0
            Removed the ``clean`` argument in favor of :paramref:`drop_pending_updates`.

        Args:
            poll_interval (:obj:`float`, optional): Time to wait between polling updates from
                Telegram in seconds. Default is ``0.0``.
            timeout (:obj:`float`, optional): Passed to :meth:`telegram.Bot.get_updates`.
            bootstrap_retries (:obj:`int`, optional): Whether the bootstrapping phase of the
                :class:`telegram.ext.Updater` will retry on failures on the Telegram server.

                * < 0 - retry indefinitely (default)
                *   0 - no retries
                * > 0 - retry up to X times
            read_timeout (:obj:`float` | :obj:`int`, optional): Grace time in seconds for receiving
                the reply from server. Will be added to the :paramref:`timeout` value and used as
                the read timeout from server. Default is ``2``.
            write_timeout (:obj:`float`, optional): The maximum amount of time (in seconds) to
                wait for a write operation to complete (in terms of a network socket;
                i.e. POSTing a request or uploading a file). :obj:`None` will set an infinite
                timeout. Defaults to :obj:`None`.
            connect_timeout (:obj:`float`, optional): The maximum amount of time (in seconds) to
                wait for a connection attempt to a server to succeed. :obj:`None` will set an
                infinite timeout for connection attempts. Defaults to :obj:`None`.
            pool_timeout (:obj:`float`, optional): The maximum amount of time (in seconds) to wait
                for a connection from the connection pool becoming available. :obj:`None` will set
                an infinite timeout. Defaults to :obj:`None`.
            allowed_updates (List[:obj:`str`], optional): Passed to
                :meth:`telegram.Bot.get_updates`.
            drop_pending_updates (:obj:`bool`, optional): Whether to clean any pending updates on
                Telegram servers before actually starting to poll. Default is :obj:`False`.

                .. versionadded :: 13.4
            error_callback (Callable[[:exc:`telegram.error.TelegramError`], :obj:`None`], \
                optional): Callback to handle :exc:`telegram.error.TelegramError` s that occur
                while calling :meth:`telegram.Bot.get_updates` during polling. Defaults to
                :obj:`None`, in which case errors will be logged.

        Returns:
            :class:`asyncio.Queue`: The update queue that can be filled from the main thread.

        Raises:
            :exc:`RuntimeError`: If the updater is already running or was not initialized.

        """
        async with self.__lock:
            if self.running:
                raise RuntimeError('This Updater is already running!')
            if not self._initialized:
                raise RuntimeError('This Updater is not initialized!')

            self._running = True

            try:
                # Create & start tasks
                polling_ready = asyncio.Event()

                await self._start_polling(
                    poll_interval=poll_interval,
                    timeout=timeout,
                    read_timeout=read_timeout,
                    write_timeout=write_timeout,
                    connect_timeout=connect_timeout,
                    pool_timeout=pool_timeout,
                    bootstrap_retries=bootstrap_retries,
                    drop_pending_updates=drop_pending_updates,
                    allowed_updates=allowed_updates,
                    ready=polling_ready,
                    error_callback=error_callback,
                )

                self._logger.debug('Waiting for polling to start')
                await polling_ready.wait()
                self._logger.debug('Polling to started')

                return self.update_queue
            except Exception as exc:
                self._running = False
                raise exc

    async def _start_polling(
        self,
        poll_interval: float,
        timeout: int,
        read_timeout: Optional[float],
        write_timeout: ODVInput[float],
        connect_timeout: ODVInput[float],
        pool_timeout: ODVInput[float],
        bootstrap_retries: int,
        drop_pending_updates: Optional[bool],
        allowed_updates: Optional[List[str]],
        ready: asyncio.Event,
        error_callback: Optional[Callable[[TelegramError], None]],
    ) -> None:

        self._logger.debug('Updater started (polling)')

        await self._bootstrap(  # Makes sure no webhook is set by calling delete_webhook
            bootstrap_retries,
            drop_pending_updates=drop_pending_updates,
            webhook_url='',
            allowed_updates=None,
        )

        self._logger.debug('Bootstrap done')

        async def polling_action_cb() -> bool:
            updates = await self.bot.get_updates(
                offset=self.last_update_id,
                timeout=timeout,
                read_timeout=read_timeout,
                connect_timeout=connect_timeout,
                write_timeout=write_timeout,
                pool_timeout=pool_timeout,
                allowed_updates=allowed_updates,
            )

            if updates:
                if not self.running:
                    self._logger.critical(
                        'Updater stopped unexpectedly. Pulled updates will be ignored and again '
                        'on restart.'
                    )
                else:
                    for update in updates:
                        await self.update_queue.put(update)
                    self.last_update_id = updates[-1].update_id + 1  # Add one to 'confirm' it

            return True  # Keep fetching updates & don't quit. Polls with poll_interval.

        def default_error_callback(exc: TelegramError) -> None:
            self._logger.exception('Exception happened while polling for updates.', exc_info=exc)

        # Start task that runs in background, pulls
        # updates from Telegram and inserts them in the update queue of the
        # Application.
        self.__polling_task = asyncio.create_task(
            self._network_loop_retry(
                action_cb=polling_action_cb,
                on_err_cb=error_callback or default_error_callback,
                description='getting Updates',
                interval=poll_interval,
            )
        )

        if ready is not None:
            ready.set()

    async def start_webhook(
        self,
        listen: str = '127.0.0.1',
        port: int = 80,
        url_path: str = '',
        cert: Union[str, Path] = None,
        key: Union[str, Path] = None,
        bootstrap_retries: int = 0,
        webhook_url: str = None,
        allowed_updates: List[str] = None,
        drop_pending_updates: bool = None,
        ip_address: str = None,
        max_connections: int = 40,
    ) -> asyncio.Queue:
        """
        Starts a small http server to listen for updates via webhook. If :paramref:`cert`
        and :paramref:`key` are not provided, the webhook will be started directly on
        http://listen:port/url_path, so SSL can be handled by another
        application. Else, the webhook will be started on
        https://listen:port/url_path. Also calls :meth:`telegram.Bot.set_webhook` as required.

        .. versionchanged:: 13.4
            :meth:`start_webhook` now *always* calls :meth:`telegram.Bot.set_webhook`, so pass
            ``webhook_url`` instead of calling ``updater.bot.set_webhook(webhook_url)`` manually.
        .. versionchanged:: 14.0
            Removed the ``clean`` argument in favor of :paramref:`drop_pending_updates` and removed
            the deprecated argument ``force_event_loop``.

        Args:
            listen (:obj:`str`, optional): IP-Address to listen on. Default ``127.0.0.1``.
            port (:obj:`int`, optional): Port the bot should be listening on. Must be one of
                :attr:`telegram.constants.SUPPORTED_WEBHOOK_PORTS`. Defaults to ``80``.
            url_path (:obj:`str`, optional): Path inside url (http(s)://listen:port/<url_path>).
                Defaults to ``''``.
            cert (:class:`pathlib.Path` | :obj:`str`, optional): Path to the SSL certificate file.
            key (:class:`pathlib.Path` | :obj:`str`, optional): Path to the SSL key file.
            drop_pending_updates (:obj:`bool`, optional): Whether to clean any pending updates on
                Telegram servers before actually starting to poll. Default is :obj:`False`.
                .. versionadded :: 13.4
            bootstrap_retries (:obj:`int`, optional): Whether the bootstrapping phase of the
                :class:`telegram.ext.Updater` will retry on failures on the Telegram server.

                * < 0 - retry indefinitely
                *   0 - no retries (default)
                * > 0 - retry up to X times
            webhook_url (:obj:`str`, optional): Explicitly specify the webhook url. Useful behind
                NAT, reverse proxy, etc. Default is derived from :paramref:`listen`,
                :paramref:`port`, :paramref:`url_path`, :paramref:`cert`, and :paramref:`key`.
            ip_address (:obj:`str`, optional): Passed to :meth:`telegram.Bot.set_webhook`.
                Defaults to :obj:`None`.
                .. versionadded :: 13.4
            allowed_updates (List[:obj:`str`], optional): Passed to
                :meth:`telegram.Bot.set_webhook`. Defaults to :obj:`None`.
            max_connections (:obj:`int`, optional): Passed to
                :meth:`telegram.Bot.set_webhook`. Defaults to ``40``.
                .. versionadded:: 13.6
        Returns:
            :class:`queue.Queue`: The update queue that can be filled from the main thread.

        Raises:
            :exc:`RuntimeError`: If the updater is already running or was not initialized.
        """
        async with self.__lock:
            if self.running:
                raise RuntimeError('This Updater is already running!')
            if not self._initialized:
                raise RuntimeError('This Updater is not initialized!')

            self._running = True

            try:
                # Create & start tasks
                webhook_ready = asyncio.Event()

                await self._start_webhook(
                    listen=listen,
                    port=port,
                    url_path=url_path,
                    cert=cert,
                    key=key,
                    bootstrap_retries=bootstrap_retries,
                    drop_pending_updates=drop_pending_updates,
                    webhook_url=webhook_url,
                    allowed_updates=allowed_updates,
                    ready=webhook_ready,
                    ip_address=ip_address,
                    max_connections=max_connections,
                )

                self._logger.debug('Waiting for webhook server to start')
                await webhook_ready.wait()
                self._logger.debug('Webhook server started')
            except Exception as exc:
                self._running = False
                raise exc

            # Return the update queue so the main thread can insert updates
            return self.update_queue

    async def _start_webhook(
        self,
        listen: str,
        port: int,
        url_path: str,
        bootstrap_retries: int,
        allowed_updates: Optional[List[str]],
        cert: Union[str, Path] = None,
        key: Union[str, Path] = None,
        drop_pending_updates: bool = None,
        webhook_url: str = None,
        ready: asyncio.Event = None,
        ip_address: str = None,
        max_connections: int = 40,
    ) -> None:
        self._logger.debug('Updater thread started (webhook)')

        if not url_path.startswith('/'):
            url_path = f'/{url_path}'

        # Create Tornado app instance
        app = WebhookAppClass(url_path, self.bot, self.update_queue)

        # Form SSL Context
        # An SSLError is raised if the private key does not match with the certificate
        # Note that we only use the SSL certificate for the WebhookServer, if the key is also
        # present. This is because the WebhookServer may not actually be in charge of performing
        # the SSL handshake, e.g. in case a reverse proxy is used
        if cert is not None and key is not None:
            try:
                ssl_ctx: Optional[ssl.SSLContext] = ssl.create_default_context(
                    ssl.Purpose.CLIENT_AUTH
                )
                ssl_ctx.load_cert_chain(cert, key)  # type: ignore[union-attr]
            except ssl.SSLError as exc:
                raise TelegramError('Invalid SSL Certificate') from exc
        else:
            ssl_ctx = None

        # Create and start server
        self._httpd = WebhookServer(listen, port, app, ssl_ctx)

        if not webhook_url:
            webhook_url = self._gen_webhook_url(
                protocol='https' if ssl_ctx else 'http',
                listen=listen,
                port=port,
                url_path=url_path,
            )

        # We pass along the cert to the webhook if present.
        if cert is not None:
            await self._bootstrap(
                cert=cert,
                max_retries=bootstrap_retries,
                drop_pending_updates=drop_pending_updates,
                webhook_url=webhook_url,
                allowed_updates=allowed_updates,
                ip_address=ip_address,
                max_connections=max_connections,
            )
        else:
            await self._bootstrap(
                max_retries=bootstrap_retries,
                drop_pending_updates=drop_pending_updates,
                webhook_url=webhook_url,
                allowed_updates=allowed_updates,
                ip_address=ip_address,
                max_connections=max_connections,
            )

        await self._httpd.serve_forever(ready=ready)

    @staticmethod
    def _gen_webhook_url(protocol: str, listen: str, port: int, url_path: str) -> str:
        # TODO: double check if this should be https in any case - the docs of start_webhook
        # say differently!
        return f'{protocol}://{listen}:{port}{url_path}'

    async def _network_loop_retry(
        self,
        action_cb: Callable[..., Coroutine],
        on_err_cb: Callable[[TelegramError], None],
        description: str,
        interval: float,
    ) -> None:
        """Perform a loop calling `action_cb`, retrying after network errors.

        Stop condition for loop: `self.running` evaluates :obj:`False` or return value of
        `action_cb` evaluates :obj:`False`.

        Args:
            action_cb (:obj:`callable`): Network oriented callback function to call.
            on_err_cb (:obj:`callable`): Callback to call when TelegramError is caught. Receives
                the exception object as a parameter.
            description (:obj:`str`): Description text to use for logs and exception raised.
            interval (:obj:`float` | :obj:`int`): Interval to sleep between each call to
                `action_cb`.

        """
        self._logger.debug('Start network loop retry %s', description)
        cur_interval = interval
        while self.running:
            try:
                try:
                    if not await action_cb():
                        break
                except RetryAfter as exc:
                    self._logger.info('%s', exc)
                    cur_interval = 0.5 + exc.retry_after
                except TimedOut as toe:
                    self._logger.debug('Timed out %s: %s', description, toe)
                    # If failure is due to timeout, we should retry asap.
                    cur_interval = 0
                except InvalidToken as pex:
                    self._logger.error('Invalid token; aborting')
                    raise pex
                except TelegramError as telegram_exc:
                    self._logger.error('Error while %s: %s', description, telegram_exc)
                    on_err_cb(telegram_exc)
                    cur_interval = self._increase_poll_interval(cur_interval)
                else:
                    cur_interval = interval

                if cur_interval:
                    await asyncio.sleep(cur_interval)

            except asyncio.CancelledError:
                self._logger.debug('Network loop retry %s was cancelled', description)
                break

    @staticmethod
    def _increase_poll_interval(current_interval: float) -> float:
        # increase waiting times on subsequent errors up to 30secs
        if current_interval == 0:
            current_interval = 1
        elif current_interval < 30:
            current_interval *= 1.5
        else:
            current_interval = min(30.0, current_interval)
        return current_interval

    async def _bootstrap(
        self,
        max_retries: int,
        webhook_url: Optional[str],
        allowed_updates: Optional[List[str]],
        drop_pending_updates: bool = None,
        cert: Union[str, Path] = None,
        bootstrap_interval: float = 1,
        ip_address: str = None,
        max_connections: int = 40,
    ) -> None:
        """Entry point for handling webhooks. :meth:`start_polling` calls this to delete any
        present webhook. :meth:`start_webhook` calls this to set a webhook using
        :meth:`telegram.Bot.set_webhook. If there are unsuccessful attempts, it will be retried as
        specified by :paramref:`max_retries`.
        """
        retries = [0]

        async def bootstrap_del_webhook() -> bool:
            self._logger.debug('Deleting webhook')
            if drop_pending_updates:
                self._logger.debug('Dropping pending updates from Telegram server')
            await self.bot.delete_webhook(drop_pending_updates=drop_pending_updates)
            return False

        async def bootstrap_set_webhook() -> bool:
            self._logger.debug('Setting webhook')
            if drop_pending_updates:
                self._logger.debug('Dropping pending updates from Telegram server')
            await self.bot.set_webhook(
                url=webhook_url,
                certificate=cert,
                allowed_updates=allowed_updates,
                ip_address=ip_address,
                drop_pending_updates=drop_pending_updates,
                max_connections=max_connections,
            )
            return False

        def bootstrap_on_err_cb(exc: Exception) -> None:
            if not isinstance(exc, InvalidToken) and (max_retries < 0 or retries[0] < max_retries):
                retries[0] += 1
                self._logger.warning(
                    'Failed bootstrap phase; try=%s max_retries=%s', retries[0], max_retries
                )
            else:
                self._logger.error('Failed bootstrap phase after %s retries (%s)', retries[0], exc)
                raise exc

        # Dropping pending updates from TG can be efficiently done with the drop_pending_updates
        # parameter of delete/start_webhook, even in the case of polling. Also, we want to make
        # sure that no webhook is configured in case of polling, so we just always call
        # delete_webhook for polling
        if drop_pending_updates or not webhook_url:
            await self._network_loop_retry(
                bootstrap_del_webhook,
                bootstrap_on_err_cb,
                'bootstrap del webhook',
                bootstrap_interval,
            )
            retries[0] = 0

        # Restore/set webhook settings, if needed. Again, we don't know ahead if a webhook is set,
        # so we set it anyhow.
        if webhook_url:
            await self._network_loop_retry(
                bootstrap_set_webhook,
                bootstrap_on_err_cb,
                'bootstrap set webhook',
                bootstrap_interval,
            )

    async def stop(self) -> None:
        """Stops the polling/webhook.

        .. seealso::
            :meth:`start_polling`, :meth:`start_webhook`

        Raises:
            :exc:`RuntimeError`: If the updater is not running.
        """
        async with self.__lock:
            if not self.running:
                raise RuntimeError('This Updater is not running!')

            self._logger.debug('Stopping Updater')

            self._running = False

            await self._stop_httpd()
            await self._stop_polling()

            self._logger.debug('Updater.stop() is complete')

    async def _stop_httpd(self) -> None:
        """Stops the Webhook server by calling ``WebhookServer.shutdown()``"""
        if self._httpd:
            self._logger.debug('Waiting for current webhook connection to be closed.')
            await self._httpd.shutdown()
            self._httpd = None

    async def _stop_polling(self) -> None:
        """Stops the polling task by awaiting it."""
        if self.__polling_task:
            self._logger.debug('Waiting background polling task to join.')
            self.__polling_task.cancel()
            try:
                await self.__polling_task
            except asyncio.CancelledError:
                pass
            self.__polling_task = None
