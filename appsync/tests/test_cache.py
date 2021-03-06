import os

from zope.interface.registry import ComponentLookupError

import vep

from appsync.tests.test_server import TestSyncApp
from appsync.cache import IAppCache, CacheError


_INI = os.path.join(os.path.dirname(__file__), 'tests_cache.ini')


class TestCache(TestSyncApp):

    ini = _INI

    def test_poll(self):
        # yeah, sorry... XXX
        registry = self.app.app.app.app.registry

        try:
            cache = registry.getUtility(IAppCache)
        except ComponentLookupError:
            return   # no cache configured

        try:
            cache.get('test')
        except CacheError:
            # memcached not running
            return

        # now the test...
        cache.set('X-Sync-Poll', 120)

        # start a session
        audience = 'http://myapps.mozillalabs.com/'
        assertion = vep.DummyVerifier.make_assertion("t@m.com", audience)
        login_data = {'assertion': assertion, "audience": audience}
        resp = self.app.post('/verify', login_data)
        res = resp.json

        # get the auth header
        auth = res["http_authorization"].encode("ascii")
        extra = {'HTTP_AUTHORIZATION': auth}

        # getting the collection 'blah'
        res = self.app.get('/collections/t@m.com/blah',
                            extra_environ=extra)

        self.assertEquals(res.headers['X-Sync-Poll'], '120')
