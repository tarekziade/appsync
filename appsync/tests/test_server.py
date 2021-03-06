import os
import unittest
import time
import json

from webtest import TestApp
from webob import exc
from webob.dec import wsgify
from pyramid import testing
from mozsvc.config import load_into_settings

import vep

from appsync import CatchAuthError
from appsync.storage import IAppSyncDatabase, ServerError
from appsync.tests.support import memcache_up


_INI = os.path.join(os.path.dirname(__file__), 'tests.ini')


class CatchErrors(object):
    def __init__(self, app):
        self.app = app

    @wsgify
    def __call__(self, request):
        try:
            return request.get_response(self.app)
        except exc.HTTPException, e:
            return e


class FakeCache(dict):
    def set(self, key, value, *args, **kw):
        self[key] = value


class TestSyncApp(unittest.TestCase):

    ini = _INI

    def setUp(self):
        self.config = testing.setUp()
        # creating a test application
        settings = {}
        load_into_settings(self.ini, settings)
        self.config.add_settings(settings)
        self.config.include("appsync")

        # do we have memcached support, if not
        # and if we use the SQL backend we need to
        # adapt its options
        if not memcache_up():
            backend = self.config.registry.settings['storage.backend']
            storage = self.config.registry.getUtility(IAppSyncDatabase)
            if backend == 'appsync.storage.sql.SQLDatabase':
                storage.cache = FakeCache()
            elif backend == 'appsync.storage.mirrored.MirroredDatabase':
                rw = storage._readwrite
                if rw.__class__.__name__ == 'SQLDatabase':
                    rw.cache = FakeCache()

        wsgiapp = self.config.make_wsgi_app()
        retry_after = self.config.registry.settings.get('global.retry_after',
                '120')
        app = CatchErrors(CatchAuthError(wsgiapp, retry_after))
        self.app = TestApp(app)

    def tearDown(self):
        sqluri = self.config.get_settings().get('storage.sqluri')
        if sqluri is not None and sqluri.startswith('sqlite'):
            filename = sqluri[len('sqlite://'):]
            if os.path.exists(filename):
                os.remove(filename)

    def test_verify(self):
        audience = "http://myapps.mozillalabs.com/"
        assertion = vep.DummyVerifier.make_assertion("t@m.com", audience)

        # missing 'audience'  => 400
        login_data = {'assertion': assertion}
        self.app.post('/verify', login_data, status=400)

        # missing 'assertion'  => 400
        login_data = {'audience': audience}
        self.app.post('/verify', login_data, status=400)

        # bad assertion
        bad_assertion = vep.DummyVerifier.make_assertion("t@m.com",
                                                         audience,
                                                         assertion_sig="BAD")
        login_data = {'assertion': bad_assertion, 'audience': audience}
        resp = self.app.post('/verify', login_data, status=401)

        # bad audience
        login_data = {'assertion': assertion, 'audience': 'http://evil.com'}
        resp = self.app.post('/verify', login_data, status=401)

        # looking good
        login_data = {'assertion': assertion, 'audience': audience}
        resp = self.app.post('/verify', login_data)
        res = resp.json

        # checking the result
        self.assertEqual(res['status'], 'okay')
        self.assertEqual(res['audience'], 'http://myapps.mozillalabs.com/')
        self.assertEqual(res['email'], 't@m.com')
        self.assertTrue(res['valid-until'] > time.time())
        self.assertTrue(res['issuer'], 'browserid.org')

    def test_protocol(self):
        # start a session
        audience = "http://myapps.mozillalabs.com/"
        assertion = vep.DummyVerifier.make_assertion("t@m.com", audience)
        login_data = {'assertion': assertion,
                      'audience': audience}
        resp = self.app.post('/verify', login_data)
        res = resp.json

        # get the auth header
        auth = res["http_authorization"].encode("ascii")
        extra = {'HTTP_AUTHORIZATION': auth}

        # try to get the collection 'blah' with no token
        self.app.get('/collections/t@m.com/blah', status=401)

        # getting the collection 'blah'
        data = self.app.get('/collections/t@m.com/blah',
                            extra_environ=extra).json
        # what did we get ?
        self.assertTrue(data['until'] <= time.time() + 0.1)
        self.assertEqual(data['since'], 0)
        self.assertEqual(len(data['applications']), 0)

        # getting the collection 'blah' since 5 min ago
        since = time.time() - 300
        data2 = self.app.get('/collections/t@m.com/blah?since=%s' % since,
                             extra_environ=extra).json

        # what did we get ?
        self.assertTrue(data2['until'] <= time.time() + 0.1)

        # XXX we need to use Decimal everywhere on server-side
        self.assertTrue(since - data2['since'] < 0.2)
        self.assertEqual(len(data['applications']), 0)

        # ok let's put some data up
        app1 = {'origin': 'app1', 'last_modified': time.time() + 0.1}
        app2 = {'origin': 'app2', 'last_modified': time.time() + 0.1}

        apps = json.dumps([app1, app2])

        res = self.app.post('/collections/t@m.com/blah', params=apps,
                            extra_environ=extra,
                            content_type='application/json')

        # see if we got them
        data = self.app.get('/collections/t@m.com/blah',
                            extra_environ=extra).json

        # what did we get ?
        self.assertTrue(data['until'] <= time.time() + 0.1)
        self.assertEqual(data['since'], 0)
        self.assertEqual(len(data['applications']), 2)

        # getting again, with since=now
        since = time.time()
        data = self.app.get('/collections/t@m.com/blah?since=%s' % since,
                             extra_environ=extra).json

        # nothing has changed, empty apps
        self.assertEqual(len(data['applications']), 0)

        # let's delete some stuff
        delete = {'client_id': 'client1',
                  'reason': 'well...'}

        self.app.post('/collections/t@m.com/blah?delete',
                      extra_environ=extra, params=json.dumps(delete),
                      content_type='application/json')

        # see if we got them
        data = self.app.get('/collections/t@m.com/blah',
                            extra_environ=extra).json

        self.assertEquals(['collection_deleted'], data.keys())

        # in case we delete and recreate the collection
        # the uuid needs to change

        # creating some data
        self.app.post('/collections/t@m.com/blah',
                      extra_environ=extra, params=apps,
                      content_type='application/json')
        data = self.app.get('/collections/t@m.com/blah',
                            extra_environ=extra).json

        uuid = data['uuid']

        # deleting that collection
        delete = {'client_id': 'client1',
                  'reason': 'well...'}

        self.app.post('/collections/t@m.com/blah?delete',
                      extra_environ=extra, params=json.dumps(delete),
                      content_type='application/json')

        # see if we got them
        data = self.app.get('/collections/t@m.com/blah',
                            extra_environ=extra).json

        self.assertEquals(['collection_deleted'], data.keys())

        # creating some data again
        self.app.post('/collections/t@m.com/blah',
                      extra_environ=extra,
                      params=apps,
                      content_type='application/json')

        data = self.app.get('/collections/t@m.com/blah',
                            extra_environ=extra).json

        new_uuid = data['uuid']
        until = data['until']
        self.assertNotEqual(uuid, new_uuid)

        # now let's try the 412
        # if lastget is used it will compare it with the
        # timestamp of the last change

        # the precision of the timestamps are .01
        time.sleep(.01)

        # let's change the data
        self.app.post('/collections/t@m.com/blah',
                      extra_environ=extra,
                      params=apps,
                      content_type='application/json')

        # let's change it again with lastget < the last change
        # we should get a 412
        self.app.post('/collections/t@m.com/blah?lastget=%s' % until,
                      extra_environ=extra,
                      params=apps,
                      content_type='application/json',
                      status=412)

    def test_heartbeat(self):
        res = self.app.get('/__heartbeat__')
        self.assertEqual(res.body, 'OK')

    def test_503(self):
        storage = self.config.registry.getUtility(IAppSyncDatabase)
        old = storage.verify

        def _break(*args):
            raise ServerError()

        storage.verify = _break

        try:
            # start a session
            login_data = {'assertion': '',
                          'audience': ''}
            res = self.app.post('/verify', login_data, status=503)
        finally:
            storage.verify = old

        wanted = self.config.registry.settings.get('global.retry_after', '120')
        self.assertEqual(res.headers['Retry-After'], str(wanted))
