import base64
import io
import json
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from PIL import Image
from fastapi.testclient import TestClient

from bridge.api import APIError
from bridge.preview import PreviewCache, PreviewParser
from bridge.service import signature
from bridge.storage import Store
from bridge.web import create_app
from test_bridge import BridgeFixture


def png():
    output = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(output, format='PNG')
    return output.getvalue()


class PreviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name, create_key=True)
        self.requests = []
        self.response = None
        def handle(request):
            self.requests.append(request)
            return self.response or httpx.Response(200, content=png())
        self.cache = PreviewCache(self.store, 'https://bridge.example', httpx.Client(transport=httpx.MockTransport(handle)))

    def tearDown(self):
        self.cache.close()
        self.store.close()
        self.tmp.cleanup()

    def test_parser_uses_exact_photo_in_album_not_avatar_reply_or_other_member(self):
        parser = PreviewParser('alexavni', 12)
        parser.feed('''<div data-post="alexavni/11">
          <img src="https://cdn4.telesco.pe/avatar.jpg">
          <a class="tgme_widget_message_photo_wrap" href="https://t.me/alexavni/11"
             style="background-image:url('https://cdn4.telesco.pe/first.jpg')"></a>
          <a class="tgme_widget_message_photo_wrap extra" href="https://t.me/alexavni/12?single"
             style="background-image:url('https://cdn4.telesco.pe/second.jpg')"></a>
          <a class="tgme_widget_message_video_thumb" href="https://t.me/alexavni/12"
             style="background-image:url('https://cdn4.telesco.pe/video.jpg')"></a></div>''')
        self.assertEqual(['https://cdn4.telesco.pe/second.jpg'], parser.images)

    def test_photo_validation_cache_and_request_bounds(self):
        self.cache.max_cache_bytes = 1
        with self.assertRaises(APIError) as caught:
            self.cache.save_image(png())
        self.assertEqual('preview_cache_full', caught.exception.code)
        self.cache.max_cache_bytes = 1024 ** 3
        url = self.cache.save_image(png())
        self.assertEqual(url, self.cache.save_image(png()))
        name = url.rsplit('/', 1)[1]
        self.assertEqual(png(), self.cache.path(name).read_bytes())
        self.assertIsNone(self.cache.path('../master.key'))
        for bad in (b'<svg></svg>', b'not an image', b'x'*(4*1024*1024+1)):
            with self.assertRaises(APIError):
                self.cache.save_image(bad)
        for url in ('http://cdn4.telesco.pe/x', 'https://localhost/x', 'https://cdn4.telesco.pe.attacker.example/x',
                    'https://user:password@cdn4.telesco.pe/x', 'https://cdn4.telesco.pe:444/x'):
            with self.assertRaises(APIError):
                self.cache.download(url, 100, cdn=True)
        self.assertEqual([], self.requests)
        with self.assertRaises(APIError):
            self.cache.download('https://cdn4.telesco.pe/x', 10, cdn=True)
        self.assertNotIn('authorization', self.requests[0].headers)
        self.assertNotIn('x-api-key', self.requests[0].headers)

    def test_public_preview_fetch_and_missing_preview(self):
        def response(request):
            if request.url.host == 't.me':
                return httpx.Response(200, text='''<a class="tgme_widget_message_photo_wrap"
                  href="https://t.me/alexavni/12"
                  style="background-image:url('https://cdn4.telesco.pe/photo.jpg')"></a>''')
            return httpx.Response(200, content=png())
        self.cache.client.close()
        self.cache.client = httpx.Client(transport=httpx.MockTransport(response))
        with patch('bridge.preview.time.sleep'):
            images = self.cache.prepare({'username': 'alexavni'}, {'id': 12})
        self.assertEqual(1, len(images))
        self.assertTrue(images[0].startswith('https://bridge.example/media/'))
        self.cache.client.close()
        self.cache.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='Unavailable')))
        self.cache.next_request = 0
        self.assertEqual([], self.cache.prepare({'username': 'alexavni'}, {'id': 12}))

    def test_redirects_and_rate_limits_do_not_trigger_more_requests(self):
        self.response = httpx.Response(302, headers={'Location': 'https://localhost/x'})
        with self.assertRaises(APIError):
            self.cache.download('https://t.me/alexavni/12', 100)
        self.response = httpx.Response(429, headers={'Retry-After': '120'})
        self.cache.next_request = 0
        with self.assertRaises(APIError) as caught:
            self.cache.download('https://t.me/alexavni/12', 100)
        self.assertEqual(120, caught.exception.retry_after)
        self.cache.close()
        self.cache = PreviewCache(self.store, 'https://bridge.example', httpx.Client(transport=httpx.MockTransport(lambda r: self.fail('Cooldown lost'))))
        with self.assertRaises(APIError) as caught:
            self.cache.download('https://t.me/alexavni/12', 100)
        self.assertEqual(429, caught.exception.status)

    def test_expired_unreferenced_photos_are_removed(self):
        image = self.cache.save_image(png())
        name = image.rsplit('/', 1)[1]
        self.cache.prune(time.time() + self.cache.retention_seconds + 1)
        self.assertIsNone(self.cache.path(name))


class PhotoQueueTest(BridgeFixture):
    def test_photo_intake_restart_retry_and_immutable_publication(self):
        self.ready()
        headers, raw = self.webhook()
        event = json.loads(raw)
        event['message']['media'] = [{'type': 'photo'}]
        raw = json.dumps(event).encode()
        headers['x-webhook-signature'] = signature('w'*40, headers['x-webhook-timestamp'], raw)
        with patch.object(self.bridge.previews, 'prepare', side_effect=AssertionError('No network in webhook')):
            self.assertEqual(204, self.bridge.telegram(100, headers, raw))
            self.assertEqual(204, self.bridge.telegram(100, headers, raw))
        self.assertEqual([], self.store.rows('SELECT * FROM outbox'))
        self.bridge.telegram(100, *self.webhook(12, 'Later post', 'later'))
        self.restart()
        with patch.object(self.bridge.previews, 'prepare', side_effect=APIError(503, 'preview_network_error')):
            self.bridge.tick()
        self.assertEqual({}, self.kobby.sent)
        self.make_due()
        image = self.bridge.previews.save_image(png())
        self.kobby.lose_send = True
        with patch.object(self.bridge.previews, 'prepare', return_value=[image]) as prepare:
            self.bridge.tick()
            self.assertEqual(1, prepare.call_count)
        saved = self.store.one('SELECT body FROM outbox WHERE message_id=11')['body']
        # Even a long publishing outage must not delete an unsent photo.
        self.bridge.previews.prune(time.time() + 10 * 86400)
        self.assertIsNotNone(self.bridge.previews.path(image.rsplit('/', 1)[1]))
        self.restart()
        self.make_due()
        with patch.object(self.bridge.previews, 'prepare', side_effect=AssertionError('Prepared photo must not be downloaded again')):
            self.bridge.tick()
            self.bridge.tick()
        self.assertEqual(2, len(self.kobby.sent))
        self.assertEqual(saved, self.store.one('SELECT body FROM outbox WHERE message_id=11')['body'])
        with TestClient(create_app(bridge=self.bridge, worker=False)) as client:
            response = client.get('/media/' + image.rsplit('/', 1)[1])
            self.assertEqual(png(), response.content)
            self.assertEqual('image/png', response.headers['content-type'])
            self.assertEqual('*', response.headers['access-control-allow-origin'])
            self.assertIn('immutable', response.headers['cache-control'])
            self.assertEqual(404, client.get('/media/not-an-image').status_code)
        expiry = self.store.one('SELECT keep_until FROM media_refs')['keep_until']
        self.assertGreater(expiry, time.time() + 2 * 86400)
        self.bridge.previews.prune(expiry + 1)
        self.assertIsNone(self.bridge.previews.path(image.rsplit('/', 1)[1]))

    def test_missing_preview_keeps_caption_without_original_link(self):
        source = self.ready()
        self.bridge.ingest(source, {'id': 11, 'text': 'Caption', 'media': [{'type': 'photo'}]})
        with patch.object(self.bridge.previews, 'prepare', return_value=[]):
            for _ in range(2):
                self.bridge.tick()
                self.assertEqual({}, self.kobby.sent)
                self.make_due()
            self.bridge.tick()
        body = next(iter(self.kobby.sent.values()))
        text = base64.b64decode(body['content']['body']).decode()
        self.assertIn('Caption', text)
        self.assertIn('Фото недоступно', text)
        self.assertNotIn('https://t.me/', text)


if __name__ == '__main__':
    unittest.main()
