"""Fetch a public post preview once and serve an immutable local photo copy."""
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import io
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
import warnings

import httpx
from PIL import Image, UnidentifiedImageError

from bridge.api import APIError


MEDIA_NAME = re.compile(r'[0-9a-f]{64}\.(?:jpg|png|webp)\Z')
CDN_HOST = re.compile(r'cdn[0-9]+\.telesco\.pe\Z')
MAX_IMAGE = 4 * 1024 * 1024
FORMATS = {'JPEG': ('jpg', 'image/jpeg'), 'PNG': ('png', 'image/png'), 'WEBP': ('webp', 'image/webp')}


def wants_photo(message):
    return any(isinstance(m, dict) and m.get('type') == 'photo' for m in message.get('media', []))


class PreviewParser(HTMLParser):
    def __init__(self, username, message_id):
        super().__init__(convert_charrefs=True)
        self.post = '/' + username.lower() + '/' + str(message_id)
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag != 'a' or 'tgme_widget_message_photo_wrap' not in attrs.get('class', '').split():
            return
        # In albums, data-post belongs to the first member. Each photo anchor
        # carries its own message ID; never copy neighbours, reply photos or avatars.
        link = urlsplit(attrs.get('href', ''))
        if link.scheme != 'https' or link.netloc != 't.me' or link.path.lower() != self.post:
            return
        match = re.search(r'background-image\s*:\s*url\(\s*[\'\"]?(https://[^\s\'\"()]+)', attrs.get('style', ''))
        if match and match[1] not in self.images:
            self.images.append(match[1])


class PreviewCache:
    def __init__(self, store, public_base, client=None, max_cache_bytes=1024 ** 3,
                 retention_seconds=3 * 86400):
        self.store, self.public_base = store, public_base
        self.max_cache_bytes = max_cache_bytes
        self.retention_seconds = retention_seconds
        self.next_cleanup = 0
        self.directory = store.directory / 'media'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.client = client or httpx.Client(follow_redirects=False, trust_env=False, timeout=5)
        self.next_request = 0

    def close(self):
        self.client.close()

    def prune(self, now=None):
        now = time.time() if now is None else now
        if now < self.next_cleanup:
            return
        protected = {row['name'] for row in self.store.rows('''SELECT DISTINCT r.name FROM media_refs r
            WHERE r.keep_until>? OR EXISTS(SELECT 1 FROM outbox o
                WHERE o.source_id=r.source_id AND o.message_id=r.message_id AND o.state!='sent')''', (now,))}
        for path in self.directory.iterdir():
            if path.is_file() and path.name not in protected:
                lifetime = 3600 if path.name.startswith('.pending-') else self.retention_seconds
                if path.stat().st_mtime + lifetime <= now:
                    path.unlink(missing_ok=True)
        self.next_cleanup = now + 3600

    def download(self, url, limit, *, cdn=False):
        try:
            parsed = urlsplit(url)
            allowed = CDN_HOST.fullmatch(parsed.hostname or '') if cdn else parsed.hostname == 't.me'
            valid = (parsed.scheme == 'https' and allowed and not parsed.username and not parsed.password
                     and parsed.port in (None, 443))
        except ValueError:
            valid = False
        if not valid:
            raise APIError(400, 'preview_url_unsupported')
        pause = self.store.one("SELECT value FROM settings WHERE key='preview_pause_until'")
        delay = float(pause['value']) - time.time() if pause else 0
        if delay > 0:
            raise APIError(429, 'preview_rate_limited', math.ceil(delay))
        time.sleep(max(0, self.next_request - time.monotonic()))
        self.next_request = time.monotonic() + 2
        started = time.monotonic()
        try:
            with self.client.stream('GET', url, headers={'User-Agent': 'KobbyTelegramBridge/1.1 (public photo previews)'}) as response:
                if response.status_code == 429:
                    value = response.headers.get('Retry-After', '60')
                    try:
                        delay = max(1, int(value))
                    except ValueError:
                        try:
                            delay = max(1, math.ceil(parsedate_to_datetime(value).timestamp() - time.time()))
                        except (ValueError, TypeError, OverflowError):
                            delay = 60
                    self.store.execute("INSERT INTO settings VALUES('preview_pause_until',?) "
                                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (str(time.time() + delay),))
                    raise APIError(429, 'preview_rate_limited', delay)
                if response.status_code != 200:
                    raise APIError(response.status_code if response.status_code >= 400 else 400, 'preview_unavailable')
                parts, length = [], 0
                for chunk in response.iter_bytes():
                    length += len(chunk)
                    if length > limit:
                        raise APIError(413, 'preview_too_large')
                    if time.monotonic() - started > 15:
                        raise APIError(503, 'preview_timeout')
                    parts.append(chunk)
                return b''.join(parts)
        except httpx.HTTPError:
            raise APIError(503, 'preview_network_error') from None

    def prepare(self, source, message):
        html = self.download(f"https://t.me/{source['username']}/{message['id']}?embed=1&mode=tme", 1024 * 1024)
        parser = PreviewParser(source['username'], message['id'])
        try:
            parser.feed(html.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise APIError(400, 'preview_invalid') from None
        if not parser.images:
            return []
        data = self.download(parser.images[0], MAX_IMAGE, cdn=True)
        return [self.save_image(data)]

    def save_image(self, data):
        if len(data) > MAX_IMAGE:
            raise APIError(413, 'preview_too_large')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as photo:
                    if photo.format not in FORMATS or photo.width > 8192 or photo.height > 8192 or photo.width * photo.height > 16000000:
                        raise APIError(400, 'preview_image_unsupported')
                    extension, _ = FORMATS[photo.format]
                    photo.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise APIError(400, 'preview_image_invalid') from None
        name = hashlib.sha256(data).hexdigest() + '.' + extension
        target = self.directory / name
        self.prune()
        if not target.exists():
            used = sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())
            if used + len(data) > self.max_cache_bytes:
                raise APIError(413, 'preview_cache_full')
            descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix='.pending-')
            try:
                with os.fdopen(descriptor, 'wb') as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, target)
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
        else:
            # A fresh post may reuse the same bytes as an older post.
            os.utime(target, None)
        return self.public_base + '/media/' + name

    def path(self, name):
        if not MEDIA_NAME.fullmatch(name):
            return None
        path = self.directory / name
        return path if path.is_file() else None
