"""Convert Telegram UTF-16 entities to bounded, self-contained Markdown parts."""
import re
from urllib.parse import quote, urlsplit


PUNCTUATION = re.compile(r'([\\`*_{}\[\]<>()#+.!|~=-])')
KINDS = {'MessageEntityBold', 'MessageEntityItalic', 'MessageEntityStrike',
         'MessageEntityCode', 'MessageEntityPre', 'MessageEntityTextUrl',
         'MessageEntityBlockquote'}


def escape(text):
    return PUNCTUATION.sub(r'\\\1', text).replace('\n', '  \n')


def destination(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return quote(value, safe=':/?#@!$&\'*+,;=%~')


class TelegramMarkdown:
    def __init__(self, text, entities):
        self.text = text
        boundaries, units = {0: 0}, 0
        for index, char in enumerate(text, 1):
            units += 2 if ord(char) > 0xffff else 1
            boundaries[units] = index
        self.entities = []
        # Bound parser work and remain below Kobby's node/depth limits.
        for entity in (entities if isinstance(entities, list) else [])[:256]:
            if not isinstance(entity, dict) or entity.get('_') not in KINDS:
                continue
            offset, length = entity.get('offset'), entity.get('length')
            if type(offset) is not int or type(length) is not int or length <= 0:
                continue
            start, end = boundaries.get(offset), boundaries.get(offset + length)
            if start is None or end is None:
                continue
            self.entities.append((start, end, entity))
        self.entities.sort(key=lambda item: (item[0], -item[1]))

    def fragment(self, start, end):
        entities = [(max(a, start), min(b, end), e) for a, b, e in self.entities if a < end and b > start]

        def visit(left, right, items, depth=0):
            if depth >= 12:
                return escape(self.text[left:right])
            result, cursor, index = [], left, 0
            while index < len(items):
                a, b, entity = items[index]
                index += 1
                if a < cursor or b > right:
                    continue
                result.append(escape(self.text[cursor:a]))
                children = []
                while index < len(items) and items[index][0] < b:
                    child = items[index]
                    index += 1
                    if child[1] <= b:
                        children.append(child)
                kind, raw = entity['_'], self.text[a:b]
                if kind in ('MessageEntityCode', 'MessageEntityPre'):
                    fence = '`' * max(3 if kind == 'MessageEntityPre' else 1,
                                      1 + max((len(m) for m in re.findall(r'`+', raw)), default=0))
                    if kind == 'MessageEntityPre':
                        language = entity.get('language', '')
                        language = language if isinstance(language, str) and re.fullmatch(r'[\w+-]{0,32}', language) else ''
                        content = f'\n\n{fence}{language}\n{raw}\n{fence}\n\n'
                    else:
                        raw = raw.replace('\n', ' ')
                        padding = ' ' if raw.startswith(('`', ' ')) or raw.endswith(('`', ' ')) else ''
                        content = fence + padding + raw + padding + fence
                else:
                    content = visit(a, b, children, depth + 1)
                    marker = {'MessageEntityBold': '**', 'MessageEntityItalic': '_',
                              'MessageEntityStrike': '~~'}.get(kind)
                    if marker and content.strip():
                        leading = content[:len(content) - len(content.lstrip())]
                        trailing = content[len(content.rstrip()):]
                        content = leading + marker + content.strip() + marker + trailing
                    elif kind == 'MessageEntityTextUrl':
                        url = destination(entity.get('url'))
                        if url:
                            content = '[' + content + '](' + url + ')'
                    elif kind == 'MessageEntityBlockquote':
                        content = '\n\n' + '\n'.join('> ' + line for line in content.split('\n')) + '\n\n'
                result.append(content)
                cursor = b
            result.append(escape(self.text[cursor:right]))
            return ''.join(result)

        return visit(start, end, entities)

    def chunks(self, limit=40 * 1024):
        chunks, start = [], 0
        while start < len(self.text):
            # Binary search on original text boundaries, regenerating balanced
            # markup in each part instead of splitting Markdown delimiters.
            low, high, best = start + 1, min(len(self.text), start + limit), start
            for count, newline in enumerate(re.finditer('\n', self.text[start:high]), 1):
                if count == 400:
                    high = start + newline.end()
                    break
            while low <= high:
                end = (low + high) // 2
                if len(self.fragment(start, end).encode()) <= limit:
                    best, low = end, end + 1
                else:
                    high = end - 1
            if best == start:
                raise ValueError('Entity exceeds publication limit')
            if best < len(self.text):
                newline = self.text.rfind('\n', start + (best - start) // 2, best)
                if newline >= 0:
                    best = newline + 1
            chunks.append(self.fragment(start, best))
            start = best
        return chunks
