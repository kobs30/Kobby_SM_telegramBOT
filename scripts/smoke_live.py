"""Explicit live acceptance check using a dedicated test account, never personal chats.

Run only when authorized to register a test account, connect the chosen public
source, and create/delete an isolated validation channel. Credentials remain in
the specified encrypted local store. No secret or post body is printed.
"""
import argparse
import json
import time
import uuid

from bridge.api import API, APIError, Publisher
from bridge.cli import load_config, provision
from bridge.storage import Store
from bridge.render import render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--test-data', required=True)
    parser.add_argument('--source', default='alexavni')
    parser.add_argument('--extra-permission', action='append', default=[],
                        help='Additional grant to test alongside communities.join; repeatable')
    parser.add_argument('--image-url', help='Optional cached public image URL to verify and include in the isolated Markdown post')
    args = parser.parse_args()
    config = load_config(args.config)
    api = API(config['kobby_base'])
    test = Store(args.test_data, create_key=True)
    test.bind('kobby_base', config['kobby_base'])
    account = provision(test, api, 'tlg_check_' + uuid.uuid4().hex[:8])
    publisher = Publisher(test, api)
    bot_id = test.secret('test_bot_id') or uuid.uuid4().hex
    test.put_secret('test_bot_id', bot_id)
    payload = {'id': bot_id, 'name': 'Telegram bridge acceptance check',
               'url': config['public_base'] + '/t.me/' + args.source,
               'chat_ids': [], 'permissions': list(dict.fromkeys(['communities.join', *args.extra_permission])),
               'enabled': True, 'allow_bots': False, 'auto_connect': True, 'all_personal_chats': False}
    try:
        publisher.request('POST', '/api/v1/bots', payload)
    except APIError as error:
        if error.status != 409:
            raise
    print(json.dumps({'test_user':account['username'],'bot_id':bot_id,'step':'waiting_for_connection'}), flush=True)
    joined = None
    try:
        deadline = time.time()+180
        while time.time() < deadline:
            bots = publisher.request('GET','/api/v1/bots')
            rows = bots.get('bots', []) if isinstance(bots,dict) else bots
            bot = next((b for b in rows if b['id']==bot_id),None)
            if bot and not bot.get('connection_pending',True) and bot.get('status')=='connected':
                chats = publisher.request('GET','/api/v1/chats')['chats']
                joined = next((c for c in chats if c.get('type')=='channel' and c.get('title','').lower()=='tlg_'+args.source.lower()),None)
                if not joined:
                    raise RuntimeError('Connected acknowledgment without mirror membership')
                print(json.dumps({'step':'connected','channel_id':joined['id'],'title':joined['title']}),flush=True)
                break
            time.sleep(3)
        if not joined:
            raise RuntimeError('Connection did not complete before deadline')
        test.put_secret('mirror_id',joined['id'])
        # Use only this test user's own disposable channel for publication checks.
        channel_id = test.secret('validation_channel_id') or uuid.uuid4().hex
        test.put_secret('validation_channel_id',channel_id)
        publisher.request('POST','/api/v1/communities/create',
                          {'id':channel_id,'type':'channel','title':'Telegram bridge API validation','description':'Temporary automated acceptance check'})
        images = []
        if args.image_url:
            if not args.image_url.startswith(config['public_base'] + '/media/'):
                raise RuntimeError('Expected an image hosted by this bridge')
            import httpx
            with httpx.Client(follow_redirects=False, trust_env=False, timeout=15) as client:
                response = client.get(args.image_url, headers={'Origin': config['kobby_base']})
                response.raise_for_status()
                if response.headers.get('access-control-allow-origin') != '*':
                    raise RuntimeError('Cached image is not accessible to Kobby web')
                import io
                from PIL import Image
                with Image.open(io.BytesIO(response.content)) as photo:
                    photo.verify()
            images.append(args.image_url)
        request = json.loads(render({'id': 1, 'username': args.source, 'channel_id': channel_id},
                                    {'id': 1, 'text': 'Isolated Markdown validation.',
                                     'entities': [{'_': 'MessageEntityBold', 'offset': 0, 'length': 8}]},
                                    account['username'], config['kobby_base'], images)[0])
        request['client_message_id'] = 'validation_' + channel_id
        first = publisher.request('POST','/api/v1/messages',request)
        second = publisher.request('POST','/api/v1/messages',request)
        if not second.get('duplicate') or first['message']['id'] != second['message']['id']:
            raise RuntimeError('Publication retry did not deduplicate')
        if first['message']['content'] != request['content']:
            raise RuntimeError('Unexpected content response normalization')
        print(json.dumps({'step':'markdown_publication_and_duplicate_verified', 'image_verified':bool(images)}),flush=True)
    finally:
        # Cleanup is scoped to resources created by this acceptance test.
        publisher.request('POST','/api/v1/bots/delete',{'id':bot_id})
        if joined:
            publisher.request('POST','/api/v1/communities/leave',{'id':joined['id']})
        channel = test.secret('validation_channel_id')
        if channel:
            publisher.request('POST','/api/v1/communities/delete',{'id':channel})
        test.close()
    print('Acceptance check passed; test bot removed, test membership left, validation channel deleted.')


if __name__ == '__main__':
    main()
