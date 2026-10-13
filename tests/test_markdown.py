import base64
import json
import unittest

from bridge.markdown import TelegramMarkdown
from bridge.render import render


class MarkdownTest(unittest.TestCase):
    def test_utf16_entities_nested_formatting_and_link(self):
        text = '😀 Bold and italic link'
        entities = [{'_' : 'MessageEntityBold', 'offset': 3, 'length': 15},
                    {'_' : 'MessageEntityItalic', 'offset': 12, 'length': 6},
                    {'_' : 'MessageEntityTextUrl', 'offset': 19, 'length': 4,
                     'url': 'https://example.com/a(b)?x=1&y=2'}]
        self.assertEqual('😀 **Bold and _italic_** [link](https://example.com/a%28b%29?x=1&y=2)',
                         TelegramMarkdown(text, entities).fragment(0, len(text)))

    def test_literal_markdown_and_unsupported_entities_remain_readable(self):
        text = '![fake](https://example.org/x.png) <script> & _word_'
        result = TelegramMarkdown(text, [{'_' : 'MessageEntityUnderline', 'offset': 0, 'length': 4}]).fragment(0, len(text))
        self.assertIn(r'\!\[fake\]', result)
        self.assertIn(r'\<script\>', result)
        self.assertIn(r'\_word\_', result)

    def test_invalid_offsets_urls_and_crossing_entities_do_not_corrupt_text(self):
        text = '😀abc'
        entities = [{'_' : 'MessageEntityBold', 'offset': 1, 'length': 1},
                    {'_' : 'MessageEntityTextUrl', 'offset': 2, 'length': 3, 'url': 'javascript:alert(1)'},
                    {'_' : 'MessageEntityBold', 'offset': -1, 'length': 100}]
        self.assertEqual(text, TelegramMarkdown(text, entities).fragment(0, len(text)))

    def test_code_quotes_and_split_parts_have_balanced_markup(self):
        text = 'a`b\nline'
        for kind in ('MessageEntityCode', 'MessageEntityPre', 'MessageEntityBlockquote'):
            result = TelegramMarkdown(text, [{'_': kind, 'offset': 0, 'length': len(text), 'language': 'python'}]).chunks()
            self.assertEqual(1, len(result))
            self.assertIn('a`b' if kind != 'MessageEntityBlockquote' else r'a\`b', result[0])
        text = 'word ' * 3000
        parts = TelegramMarkdown(text, [{'_': 'MessageEntityBold', 'offset': 0, 'length': len(text)}]).chunks(1024)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part.encode()), 1024)
            self.assertEqual(2, part.count('**'))

    def test_markdown_photo_caption_and_no_generated_attribution(self):
        source = {'id': 100, 'username': 'alexavni', 'channel_id': 'a'*32}
        message = {'id': 11, 'text': 'Caption', 'media': [{'type': 'photo'}]}
        url = 'https://bridge.example/media/' + '1'*64 + '.jpg'
        request = json.loads(render(source, message, 'tlg_bridge', 'https://kobby.example', [url])[0])
        text = base64.b64decode(request['content']['body']).decode()
        self.assertEqual('Caption\n\n![Фото](' + url + ')', text)
        self.assertEqual('text/markdown; charset=utf-8', request['content']['content_type'])
        message['text'] = ''
        text = base64.b64decode(json.loads(render(source, message, 'tlg_bridge', 'https://kobby.example', [url])[0])['content']['body']).decode()
        self.assertEqual('![Фото](' + url + ')', text)


if __name__ == '__main__':
    unittest.main()
