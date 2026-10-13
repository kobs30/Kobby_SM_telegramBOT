import base64
import hashlib
import json
import re
from bridge.markdown import TelegramMarkdown, destination


USERNAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{3,31}\Z')


def render(source, message, sender, server, images=()):
    """Freeze Markdown and stable requests after optional preview preparation."""
    mid = message['id']
    if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
        raise ValueError('Invalid message ID')
    text = message.get('text') or ''
    if not isinstance(text, str):
        raise ValueError('Invalid message text')
    text.encode('utf-8')
    if len(text.encode()) > 1024 * 1024:
        raise ValueError('Message text exceeds intake limit')
    chunks = TelegramMarkdown(text, message.get('entities', [])).chunks() if text.strip() else []
    picture_lines = ['![Фото](' + destination(url) + ')' for url in images if destination(url)]
    if picture_lines:
        # The preview adapter returns one photo per Telegram message (album
        # members have separate IDs). Bound output for future adapters as well.
        for offset in range(0, len(picture_lines), 8):
            pictures = '\n\n'.join(picture_lines[offset:offset + 8])
            if chunks and len((chunks[-1] + '\n\n' + pictures).encode()) <= 46 * 1024:
                chunks[-1] += '\n\n' + pictures
            else:
                chunks.append(pictures)
    elif any(isinstance(m, dict) and m.get('type') == 'photo' for m in message.get('media', [])):
        notice = 'Фото недоступно в публичном превью.'
        if chunks:
            chunks[-1] += '\n\n' + notice
        else:
            chunks.append(notice)
    if not chunks:
        chunks = ['Публикация без текста. Этот тип вложения пока не поддерживается.']
    requests = []
    for part, chunk in enumerate(chunks, 1):
        content = (f'Часть {part}/{len(chunks)}\n\n' if len(chunks) > 1 else '') + chunk
        identity = json.dumps([server, source['id'], mid, part], separators=(',', ':'))
        request = {'chat_id': source['channel_id'], 'sender_id': sender,
                   'recipient_id': 'community:' + source['channel_id'],
                   'client_message_id': 'tg_' + hashlib.sha256(identity.encode()).hexdigest(),
                   'content': {'version': 1, 'content_type': 'text/markdown; charset=utf-8',
                               'body': base64.b64encode(content.encode()).decode(), 'transforms': []}}
        body = json.dumps(request, sort_keys=True, separators=(',', ':'))
        if len(content.encode()) > 48 * 1024 or len(body.encode()) > 128 * 1024:
            raise ValueError('Rendered publication exceeds Kobby limits')
        requests.append(body)
    return requests
