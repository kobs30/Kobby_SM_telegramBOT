import base64
import hashlib
import json
import re
from urllib.parse import urlsplit


USERNAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{3,31}\Z')


def render(source, message, sender, server):
    """Freeze readable text and stable requests; never download media."""
    mid = message['id']
    if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
        raise ValueError('Invalid message ID')
    text = message.get('text') or ''
    if not isinstance(text, str):
        raise ValueError('Invalid message text')
    text.encode('utf-8')
    if len(text.encode()) > 1024 * 1024:
        raise ValueError('Message text exceeds intake limit')
    links = []
    for entity in message.get('entities', []):
        url = entity.get('url', '') if isinstance(entity, dict) else ''
        if isinstance(url, str) and urlsplit(url).scheme in ('http', 'https') and url not in text and url not in links:
            links.append(url)
    if links:
        text += '\n\n' + '\n'.join(links)
    if not text.strip():
        text = 'Публикация без текста. Медиа доступны в оригинале.'
    original = f"https://t.me/{source['username']}/{mid}"
    footer = '\n\nОригинал: ' + original
    # Keep enough room for source, part number and original link on every part.
    limit = 48 * 1024 - 1024
    chunks = []
    while text:
        raw = text.encode()
        if len(raw) <= limit:
            chunks.append(text)
            break
        piece = raw[:limit].decode('utf-8', errors='ignore')
        boundary = piece.rfind('\n', len(piece) // 2)
        if boundary >= 0:
            piece = piece[:boundary + 1]
        chunks.append(piece)
        text = text[len(piece):]
    requests = []
    for part, chunk in enumerate(chunks, 1):
        heading = f"Telegram · @{source['username']}"
        if len(chunks) > 1:
            heading += f' · {part}/{len(chunks)}'
        content = heading + '\n\n' + chunk + footer
        identity = json.dumps([server, source['id'], mid, part], separators=(',', ':'))
        request = {'chat_id': source['channel_id'], 'sender_id': sender,
                   'recipient_id': 'community:' + source['channel_id'],
                   'client_message_id': 'tg_' + hashlib.sha256(identity.encode()).hexdigest(),
                   'content': {'version': 1, 'content_type': 'text/plain; charset=utf-8',
                               'body': base64.b64encode(content.encode()).decode(), 'transforms': []}}
        body = json.dumps(request, sort_keys=True, separators=(',', ':'))
        if len(content.encode()) > 48 * 1024 or len(body.encode()) > 128 * 1024:
            raise ValueError('Rendered publication exceeds Kobby limits')
        requests.append(body)
    return requests
