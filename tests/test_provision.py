import copy
import tempfile
import time
import unittest

from bridge.api import APIError
from bridge.cli import provision
from bridge.storage import Store


class Accounts:
    def __init__(self):
        self.accounts = {}
        self.lose_response = False

    def request(self, method, path, body=None, token=None):
        if path == '/api/v1/me':
            return {'id': token.split(':',1)[1], 'kind': 'user'}
        username = body['username']
        if path == '/api/v1/auth/register':
            if username in self.accounts:
                raise APIError(409, 'username_taken')
            self.accounts[username] = copy.deepcopy(body)
            if self.lose_response:
                self.lose_response = False
                raise APIError(503, 'network_error')
        elif self.accounts.get(username) != body:
            raise APIError(401, 'invalid_credentials')
        return {'user_id':username, 'token':'user:'+username, 'expires_at':time.time()+3600}


class ProvisionTest(unittest.TestCase):
    def test_registration_loss_recovered_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory,create_key=True)
            api = Accounts()
            api.lose_response = True
            with self.assertRaises(APIError):
                provision(store, api, 'tlg_bridge')
            password = store.secret('publisher')['password']
            store.close()
            store = Store(directory)
            account = provision(store, api, 'tlg_bridge')
            self.assertEqual('tlg_bridge', account['username'])
            self.assertEqual(password, account['password'])
            self.assertEqual(1,len(api.accounts))
            self.assertEqual('tlg_bridge', provision(store,api,'tlg_bridge')['username'])
            store.close()

    def test_foreign_account_gets_random_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory,create_key=True)
            api = Accounts()
            api.accounts['tlg_bridge'] = {'username':'tlg_bridge','password':'someone-elses-password'}
            account = provision(store,api,'tlg_bridge')
            self.assertRegex(account['username'], r'^tlg_bridge_[0-9]+$')
            self.assertEqual(2,len(api.accounts))
            self.assertEqual('someone-elses-password',api.accounts['tlg_bridge']['password'])
            store.close()

    def test_registration_disabled_does_not_discard_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory,create_key=True)
            class Disabled(Accounts):
                def request(self,method,path,body=None,token=None):
                    if path.endswith('/register'):
                        raise APIError(403,'registration_disabled')
                    return super().request(method,path,body,token)
            with self.assertRaises(APIError):
                provision(store,Disabled(),'tlg_bridge')
            self.assertEqual('tlg_bridge',store.secret('publisher')['username'])
            self.assertNotIn('token',store.secret('publisher'))
            store.close()
