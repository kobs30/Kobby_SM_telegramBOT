import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import html
import os
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from bridge.api import API
from bridge.cli import load_config
from bridge.render import USERNAME
from bridge.service import Bridge
from bridge.storage import Store


def create_app(config=None, bridge=None, *, worker=True):
    @asynccontextmanager
    async def lifespan(app):
        os.umask(0o077)
        instance = bridge
        if instance is None:
            cfg = config or load_config(os.environ.get('BRIDGE_CONFIG', '/config/config.json'))
            store = Store(cfg['data_dir'], key_path=cfg['key_file'])
            instance = Bridge(cfg, store, API(cfg['kobby_base']), API(cfg['watcher_base'], 'X-API-Key'))
        app.state.bridge = instance
        thread = threading.Thread(target=instance.run, name='bridge-worker', daemon=True)
        if worker:
            thread.start()
        try:
            yield
        finally:
            instance.stop.set()
            if worker:
                await asyncio.to_thread(thread.join, 120)
            if bridge is None and not thread.is_alive():
                instance.kobby.client.close()
                instance.watcher.client.close()
                instance.previews.close()
                instance.store.close()

    app = FastAPI(title='Kobby Telegram Bridge', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    attempts = defaultdict(deque)

    @app.middleware('http')
    async def protections(request, call_next):
        if request.method == 'POST' and request.url.path.startswith('/t.me/'):
            now = time.monotonic()
            # A bounded global admission limit protects the exchange endpoint.
            bucket = attempts['connections']
            while bucket and bucket[0] < now - 60:
                bucket.popleft()
            if len(bucket) >= 120:
                return Response(status_code=429, headers={'Retry-After': '60'})
            bucket.append(now)
        response = await call_next(request)
        response.headers.setdefault('Cache-Control', 'no-store')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        return response

    async def body(request):
        raw = bytearray()
        try:
            async with asyncio.timeout(8):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        return None
        except (TimeoutError, RuntimeError):
            return None
        return bytes(raw)

    @app.get('/healthz')
    def health():
        b = app.state.bridge
        b.store.one('SELECT 1')
        alive = time.time() - b.last_tick < 180 and not b.worker_error
        return JSONResponse({'status': 'ok' if alive else 'degraded'}, status_code=200 if alive else 503)

    @app.post('/t.me/{username}')
    async def kobby_hook(username: str, request: Request):
        raw = await body(request)
        if not raw:
            return Response(status_code=413)
        status = await run_in_threadpool(app.state.bridge.connect, username, dict(request.headers), raw)
        return Response(status_code=status)

    @app.post('/events/telegram/{channel_id}')
    async def telegram_hook(channel_id: int, request: Request):
        raw = await body(request)
        if not raw:
            return Response(status_code=413)
        status = await run_in_threadpool(app.state.bridge.telegram, channel_id, dict(request.headers), raw)
        return Response(status_code=status)

    @app.api_route('/media/{name}', methods=['GET', 'HEAD'])
    def photo(name: str):
        path = app.state.bridge.previews.path(name)
        if path is None:
            return Response(status_code=404)
        return FileResponse(path, headers={'Access-Control-Allow-Origin': '*',
                                          'Cache-Control': 'public, max-age=3600, immutable'})

    @app.get('/', response_class=HTMLResponse)
    def index():
        return page('Telegram → Kobby', '<p>Добавьте бота в Kobby с адресом этого сервиса, '
                    'добавив <code>/t.me/имя_канала</code>. Включите автоматическое подключение '
                    'и право «Вступать в сообщества».</p><p>Новые публикации появятся в общем публичном канале Kobby.</p>')

    @app.get('/t.me/{username}', response_class=HTMLResponse)
    def source_page(username: str):
        if not USERNAME.fullmatch(username):
            return HTMLResponse(page('Некорректный источник', '<p>Нужно публичное имя Telegram-канала.</p>'), status_code=400)
        b = app.state.bridge
        source = b.store.one('SELECT * FROM sources WHERE lower(username)=?', (username.lower(),))
        url = html.escape(b.config['public_base'] + '/t.me/' + username)
        details = '<p>Источник ещё не подключён.</p>'
        if source:
            state = {'ready': 'Канал подготовлен', 'creating': 'Канал создаётся',
                     'mirror_unavailable': 'Канал недоступен: требуется проверка оператором'}[source['state']]
            details = f'<p>{state}: <strong>tlg_{html.escape(source["username"])}</strong></p>'
            details += '<p>ID канала Kobby: <code>' + html.escape(source['channel_id']) + '</code></p>'
        return page('@' + username, '<p>Webhook URL:</p><p><code>' + url + '</code></p>' +
                    '<ol><li>В Kobby откройте «Настройки → Добавить бота».</li>'
                    '<li>Вставьте URL, включите автоматическое подключение и «Вступать в сообщества».</li>'
                    '<li>Сохраните и дождитесь статуса «Сервис подключён». Канал появится после синхронизации.</li></ol>' +
                    details + '<p>Состояние канала на этой странице общее. Ваша подписка подтверждается в Kobby.</p>' +
                    '<p>Переносим новые посты с Markdown и фото из публичных превью. '
                    'В Kobby фото открывается кнопкой «Загрузить картинку». Видео и файлы пока не переносятся. '
                    'Источник хранит только пять последних постов: длительный простой может привести к пропускам.</p>')

    return app


def page(title, content):
    return ('<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>' + html.escape(title) + '</title><style>body{font:18px/1.6 system-ui;max-width:760px;'
            'margin:60px auto;padding:0 24px;color:#172b35;background:#f4f8fa}code{overflow-wrap:anywhere}'
            'li{margin:12px 0}h1{line-height:1.2}</style><h1>' + html.escape(title) + '</h1>' + content + '</html>')


app = create_app()
