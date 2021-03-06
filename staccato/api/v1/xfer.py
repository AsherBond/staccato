import json
import logging
import urlparse
import uuid

import routes
import webob
import webob.exc

import staccato.openstack.common.wsgi as os_wsgi
import staccato.openstack.common.middleware.context as os_context
import staccato.xfer.executor as executor
import staccato.xfer.events as xfer_events
from staccato import db
from staccato.xfer.constants import Events
from staccato.common import config, exceptions
from staccato.common import utils

LOG = logging.getLogger(__name__)


def _make_request_id(user, tenant):
    return str(uuid.uuid4())


class UnauthTestMiddleware(os_context.ContextMiddleware):
    def __init__(self, app, options):
        self.options = options
        super(UnauthTestMiddleware, self).__init__(app, options)

    def process_request(self, req):
        LOG.debug('Making an unauthenticated context.')
        req.context = self.make_context(is_admin=True,
                                        user='admin')
        req.context.owner = 'admin'


class AuthContextMiddleware(os_context.ContextMiddleware):
    def __init__(self, app, options):
        self.options = options
        print options
        super(AuthContextMiddleware, self).__init__(app, options)

    def process_request(self, req):
        if req.headers.get('X-Identity-Status') == 'Confirmed':
            req.context = self._get_authenticated_context(req)
        else:
            raise webob.exc.HTTPUnauthorized()

    def _get_authenticated_context(self, req):
        LOG.debug('Making an authenticated context.')
        auth_token = req.headers.get('X-Auth-Token')
        user = req.headers.get('X-User-Id')
        tenant = req.headers.get('X-Tenant-Id')
        is_admin = self.options.admin_user_id.strip().lower() == user

        request_id = _make_request_id(user, tenant)

        context = self.make_context(is_admin=is_admin, user=user,
                                    tenant=tenant, auth_token=auth_token,
                                    request_id=request_id)
        context.owner = user

        return context


class XferController(object):

    def __init__(self, db_con, sm, conf):
        self.sm = sm
        self.db_con = db_con
        self.log = logging
        self.conf = conf

    def _xfer_from_db(self, xfer_id, owner):
        return self.db_con.lookup_xfer_request_by_id(
            xfer_id, owner=owner)

    def _to_state_machine(self, event, xfer_request, name):
        self.sm.event_occurred(event,
                               xfer_request=xfer_request,
                               db=self.db_con)

    @utils.StaccatoErrorToHTTP('Create a new transfer', LOG)
    def newtransfer(self, request, source_url, destination_url, owner,
                    source_options=None, destination_options=None,
                    start_offset=0, end_offset=None):

        srcurl_parts = urlparse.urlparse(source_url)
        dsturl_parts = urlparse.urlparse(destination_url)

        dstopts = {}
        srcopts = {}

        if source_options is not None:
            srcopts = source_options
        if destination_options is not None:
            dstopts = destination_options

        plugin_policy = config.get_protocol_policy(self.conf)
        src_module_name = utils.find_protocol_module_name(plugin_policy,
                                                          srcurl_parts)
        dst_module_name = utils.find_protocol_module_name(plugin_policy,
                                                          dsturl_parts)

        src_module = utils.load_protocol_module(src_module_name, self.conf)
        dst_module = utils.load_protocol_module(dst_module_name, self.conf)

        dstopts = dst_module.new_write(dsturl_parts, dstopts)
        srcopts = src_module.new_read(srcurl_parts, srcopts)

        xfer = self.db_con.get_new_xfer(owner,
                                        source_url,
                                        destination_url,
                                        src_module_name,
                                        dst_module_name,
                                        start_ndx=start_offset,
                                        end_ndx=end_offset,
                                        source_opts=srcopts,
                                        dest_opts=dstopts)
        return xfer

    @utils.StaccatoErrorToHTTP('Check the status', LOG)
    def status(self, request, xfer_id, owner):
        xfer = self._xfer_from_db(xfer_id, owner)
        return xfer

    @utils.StaccatoErrorToHTTP('List transfers', LOG)
    def list(self, request, owner, limit=None):
        return self.db_con.lookup_xfer_request_all(owner=owner, limit=limit)

    @utils.StaccatoErrorToHTTP('Delete a transfer', LOG)
    def delete(self, request, xfer_id, owner):
        xfer_request = self._xfer_from_db(xfer_id, owner)
        self._to_state_machine(Events.EVENT_DELETE,
                               xfer_request,
                               'delete')

    @utils.StaccatoErrorToHTTP('Cancel a transfer', LOG)
    def xferaction(self, request, xfer_id, owner, xferaction, **kwvals):
        xfer_request = self._xfer_from_db(xfer_id, owner)
        self._to_state_machine(Events.EVENT_CANCEL,
                               xfer_request,
                               'cancel')


class XferHeaderDeserializer(os_wsgi.RequestHeadersDeserializer):
    def default(self, request):
        return {'owner': request.context.owner}


class XferDeserializer(os_wsgi.JSONDeserializer):
    """Default request headers deserializer"""

    def _validate(self, body, required, optional):
        request = {}
        for k in body:
            if k not in required and k not in optional:
                msg = '%s is an unknown option.' % k
                raise webob.exc.HTTPBadRequest(explanation=msg)
        for k in required:
            if k not in body:
                msg = 'The option %s must be specified.' % k
                raise webob.exc.HTTPBadRequest(explanation=msg)
            request[k] = body[k]
        for k in optional:
            if k in body:
                request[k] = body[k]
        return request

    def newtransfer(self, body):
        _required = ['source_url', 'destination_url']
        _optional = ['source_options', 'destination_options', 'start_offset',
                     'end_offset']
        request = self._validate(self._from_json(body), _required, _optional)
        return request

    def list(self, body):
        _required = []
        _optional = ['limit', 'next', 'filter']
        request = self._validate(self._from_json(body), _required, _optional)
        return request

    def cancel(self, body):
        _required = ['xferaction']
        _optional = ['async']
        request = self._validate(body, _required, _optional)
        return request

    def xferaction(self, body):
        body = self._from_json(body)
        actions = {'cancel': self.cancel}
        action_key = 'xferaction'
        if action_key not in body:
            msg = 'You must have an action entry in the body with one of ' \
                  'the following %s' % str(actions)
            raise webob.exc.HTTPBadRequest(explanation=msg)
        action = body[action_key]
        if action not in actions:
            msg = '%s is not a valid action.' % action
            raise webob.exc.HTTPBadRequest(explanation=msg)
        func = actions[action]
        return func(body)


class XferSerializer(os_wsgi.JSONDictSerializer):

    def serialize(self, data, action='default', *args):
        return super(XferSerializer, self).serialize(data, args[0])

    def _xfer_to_json(self, data):
        x = data
        d = {}
        d['id'] = x.id
        d['source_url'] = x.srcurl
        d['destination_url'] = x.dsturl
        d['state'] = x.state
        d['start_offset'] = x.start_ndx
        d['end_offset'] = x.end_ndx
        d['progress'] = x.next_ndx
        d['source_options'] = x.source_opts
        d['destination_options'] = x.dest_opts
        return d

    def default(self, data):
        d = self._xfer_to_json(data)
        return json.dumps(d)

    def list(self, data):
        xfer_list = []
        for xfer in data:
            xfer_list.append(self._xfer_to_json(xfer))
        return json.dumps(xfer_list)


class API(os_wsgi.Router):

    def __init__(self, conf):

        self.conf = conf
        self.db_con = db.StaccatoDB(conf)

        self.executor = executor.SimpleThreadExecutor(self.conf)
        self.sm = xfer_events.XferStateMachine(self.executor)

        controller = XferController(self.db_con, self.sm, self.conf)
        mapper = routes.Mapper()

        body_deserializers = {'application/json': XferDeserializer()}
        deserializer = os_wsgi.RequestDeserializer(
            body_deserializers=body_deserializers,
            headers_deserializer=XferHeaderDeserializer())
        serializer = XferSerializer()
        transfer_resource = os_wsgi.Resource(controller,
                                             deserializer=deserializer,
                                             serializer=serializer)

        mapper.connect('/transfers',
                       controller=transfer_resource,
                       action='newtransfer',
                       conditions={'method': ['POST']})
        mapper.connect('/transfers',
                       controller=transfer_resource,
                       action='list',
                       conditions={'method': ['GET']})
        mapper.connect('/transfers/{xfer_id}',
                       controller=transfer_resource,
                       action='status',
                       conditions={'method': ['GET']})
        mapper.connect('/transfers/{xfer_id}',
                       controller=transfer_resource,
                       action='delete',
                       conditions={'method': ['DELETE']})
        mapper.connect('/transfers/{xfer_id}/action',
                       controller=transfer_resource,
                       action='xferaction',
                       conditions={'method': ['POST']})

        super(API, self).__init__(mapper)
