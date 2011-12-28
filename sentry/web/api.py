"""
sentry.web.views
~~~~~~~~~~~~~~~~

:copyright: (c) 2010 by the Sentry Team, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""

import base64
import datetime
import logging
import time
import zlib

from django.http import HttpResponse
from django.utils.encoding import smart_str
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from sentry.conf import settings
from sentry.exceptions import InvalidData, InvalidInterface
from sentry.models import Group, ProjectMember
from sentry.utils import is_float, json
from sentry.utils.auth import get_signature, parse_auth_header

logger = logging.getLogger(__name__)

class APIError(Exception):
    http_status = 400
    msg = 'Invalid request'
    def __init__(self, msg=None):
        if msg:
            self.msg = msg
    def as_http_response(self):
        return HttpResponse(self.msg, status=self.http_status)
class APIUnauthorized(APIError):
    http_status = 401
    msg = 'Unauthorized'
class APIForbidden(APIError):
    http_status = 403
class APITimestampExpired(APIError):
    http_status = 410

def extract_auth_vars(request):
    if request.META.get('HTTP_X_SENTRY_AUTH', '').startswith('Sentry'):
        # Auth version 3.0 (same as 2.0, diff header)
        return parse_auth_header(request.META['HTTP_X_SENTRY_AUTH'])
    elif request.META.get('HTTP_AUTHORIZATION', '').startswith('Sentry'):
        # Auth version 2.0
        return parse_auth_header(request.META['HTTP_AUTHORIZATION'])
    else:
        return None

def project_from_auth_vars(auth_vars, data):
    signature = auth_vars.get('sentry_signature')
    timestamp = auth_vars.get('sentry_timestamp')
    api_key = auth_vars.get('sentry_key')
    if not signature or not timestamp:
        raise APIUnauthorized()

    if api_key:
        try:
            pm = ProjectMember.objects.get(api_key=api_key)
            if not pm.has_perm('add_message'):
                raise ProjectMember.DoesNotExist
        except ProjectMember.DoesNotExist:
            raise APIForbidden('Invalid signature')
        project = pm.project
        secret_key = pm.secret_key
    else:
        project = None
        secret_key = settings.KEY

    validate_hmac(data, signature, timestamp, secret_key)

    return project

def validate_hmac(message, signature, timestamp, secret_key):
    try:
        timestamp_float = float(timestamp)
    except ValueError:
        raise APIError('Invalid timestamp')

    if timestamp_float < time.time() - 3600:  # 1 hour
        raise APITimestampExpired('Message has expired')

    sig_hmac = get_signature(message, timestamp, secret_key)
    if sig_hmac != signature:
        raise APIForbidden('Invalid signature')

def project_from_api_key_and_id(api_key, project):
    try:
        pm = ProjectMember.objects.get(api_key=api_key, project=project)
    except ProjectMember.DoesNotExist:
        raise APIUnauthorized()

    if not pm.has_perm('add_message'):
        raise ProjectMember.DoesNotExist

    return pm.project

def project_from_id(request):
    try:
        pm = ProjectMember.objects.get(user=request.user, project=request.GET['project_id'])
        # TODO: do we need this check?
        # if not pm.has_perm('add_message'):
        #     raise ProjectMember.DoesNotExist
    except ProjectMember.DoesNotExist:
        raise APIUnauthorized()

    return pm.project

def decode_and_decompress_data(encoded_data):
    try:
        try:
            return base64.b64decode(encoded_data).decode('zlib')
        except zlib.error:
            return base64.b64decode(encoded_data)
    except Exception, e:
        # This error should be caught as it suggests that there's a
        # bug somewhere in the client's code.
        logger.exception('Bad data received')
        raise APIForbidden('Bad data decoding request (%s, %s)' % (e.__class__.__name__, e))

def safely_load_json_string(json_string):
    try:
        obj = json.loads(json_string)
    except Exception, e:
        # This error should be caught as it suggests that there's a
        # bug somewhere in the client's code.
        logger.exception('Bad data received')
        raise APIForbidden('Bad data reconstructing object (%s, %s)' % (e.__class__.__name__, e))

    # XXX: ensure keys are coerced to strings
    return dict((smart_str(k), v) for k, v in obj.iteritems())


def ensure_valid_project_id(desired_project, data):
    # Confirm they're using either the master key, or their specified project matches with the
    # signed project.
    if desired_project and str(data.get('project', '')) != str(desired_project.pk):
        raise APIForbidden('Invalid credentials')
    elif not desired_project:
        data['project'] = 1

def insert_data_to_database(data):
    def process_data_timestamp(data):
        if is_float(data['timestamp']):
            try:
                data['timestamp'] = datetime.datetime.fromtimestamp(float(data['timestamp']))
            except:
                logger.exception('Failed reading timestamp')
                del data['timestamp']
        elif not isinstance(data['timestamp'], datetime.datetime):
            if '.' in data['timestamp']:
                format = '%Y-%m-%dT%H:%M:%S.%f'
            else:
                format = '%Y-%m-%dT%H:%M:%S'
            if 'Z' in data['timestamp']:
                # support UTC market, but not other timestamps
                format += 'Z'
            try:
                data['timestamp'] = datetime.datetime.strptime(data['timestamp'], format)
            except:
                logger.exception('Failed reading timestamp')
                del data['timestamp']
    if 'timestamp' in data:
        process_data_timestamp(data)

    try:
        Group.objects.from_kwargs(**data)
    except (InvalidInterface, InvalidData), e:
        raise APIError(e)

@csrf_exempt
@require_http_methods(['POST'])
def store(request):
    try:
        auth_vars = extract_auth_vars(request)
        data = request.raw_post_data

        if auth_vars:
            project = project_from_auth_vars(auth_vars, data)
        elif request.GET.get('api_key') and request.GET.get('project_id') and request.is_secure():
            # ssl requests dont have to have signature verification
            project = project_from_api_key_and_id(request.GET['api_key'], request.GET['project_id'])
        elif request.GET.get('project_id') and request.user.is_authenticated():
            # authenticated users are simply trusted to provide the right id
            project = project_from_id(request)
        else:
            raise APIUnauthorized()

        if not data.startswith('{'):
            data = decode_and_decompress_data(data)
        data = safely_load_json_string(data)

        ensure_valid_project_id(project, data)

        insert_data_to_database(data)
    except APIError, error:
        return error.as_http_response()
    return HttpResponse('')
    
