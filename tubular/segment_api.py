"""
Segment API call wrappers
"""
import logging

import backoff
import requests
from six import text_type


# These are the keys in the learner dict that contain IDs we need to retire from Segment
IDENTIFYING_KEYS = ['id', 'original_username', 'ecommerce_segment_id']

# The Segment GraphQL mutation for authorization
AUTH_MUTATION = "mutation auth($email:String!, $password:String!) {login(email:$email, password:$password)}"

# The Segment GraphQL mutation for bulk deleting users for a particular workspace
BULK_DELETE_MUTATION_OPNAME = 'createWorkspaceBulkDeletion'
BULK_DELETE_MUTATION = """
mutation {{{{
  {}(
    workspaceSlug: "{}"
    userIds: {}
  ) {{{{
    id
  }}}}
}}}}""".format(BULK_DELETE_MUTATION_OPNAME, '{}', '{}')

# The Segment GraphQL mutation for querying the status of a bulk user deletion request for a particular workspace
BULK_DELETE_STATUS_QUERY_OPNAME = 'bulkDeletion'
BULK_DELETE_STATUS_QUERY = """
query {{{{
  {}(
    id: "{}"
  ) {{{{
    id
    status
  }}}}
}}}}""".format(BULK_DELETE_STATUS_QUERY_OPNAME, '{}')

# According to Segment, represents the maximum limits of the bulk delete mutation call.
MAXIMUM_USERS_IN_DELETE_REQUEST = 16 * 1024  # 16k users

LOG = logging.getLogger(__name__)


def _backoff_handler(details):
    """
    Simple logging handler for when timeout backoff occurs.
    """
    LOG.info('Trying again in {wait:0.1f} seconds after {tries} tries calling {target}'.format(**details))


def _wait_30_seconds():
    """
    Backoff generator that waits for 30 seconds.
    """
    return backoff.constant(interval=30)


def _exception_not_internal_svr_error(exc):
    """
    Giveup method that gives up backoff upon any non-5xx and 504 server errors.
    """
    return not 500 <= exc.response.status_code < 600


def _retry_segment_api():
    """
    Decorator which enables retries with sane backoff defaults
    """
    def inner(func):  # pylint: disable=missing-docstring
        func_with_backoff = backoff.on_exception(
            backoff.expo,
            requests.exceptions.HTTPError,
            max_time=90,  # in seconds
            giveup=_exception_not_internal_svr_error,
            on_backoff=lambda details: _backoff_handler(details)  # pylint: disable=unnecessary-lambda
        )
        func_with_timeout_backoff = backoff.on_exception(
            _wait_30_seconds,
            requests.exceptions.Timeout,
            max_tries=4,
            on_backoff=lambda details: _backoff_handler(details)  # pylint: disable=unnecessary-lambda
        )
        return func_with_backoff(func_with_timeout_backoff(func))
    return inner


class SegmentApi(object):
    """
    Segment API client with convenience methods
    """
    def __init__(self, base_url, auth_email, auth_password, projects_to_retire, workspace_slug):
        self.base_url = base_url
        self.auth_email = auth_email
        self.auth_password = auth_password
        self.projects_to_retire = projects_to_retire
        self.workspace_slug = workspace_slug

    def _get_auth_token(self):
        """
        Makes the request to get an auth token and return it
        """
        mutation = {
            'query': AUTH_MUTATION,
            'variables':
                {
                    "email": "{email}".format(email=self.auth_email),
                    "password": "{password}".format(password=self.auth_password)
                }
        }

        resp = None
        resp_json = None
        try:
            resp = requests.post(self.base_url, json=mutation)
            resp_json = resp.json()
            return resp_json['data']['login']['access_token']
        except (TypeError, KeyError):
            LOG.error('Error occurred getting access token. Response {}'.format(text_type(resp)))
            LOG.error('Response JSON: {}'.format(text_type(resp_json)))
            raise

    @_retry_segment_api()
    def _call_segment_graphql(self, mutation):
        """
        Actually makes the Segment GraphQL call.

        5xx errors and timeouts will be retried via _retry_segment_api,
        all others will bubble up.

        We get the access token here instead of setting it up ahead of time
        or in __init__ because these tokens seem to be very short-lived. If a
        previous retirement step runs long, or if there are numerous retries,
        the token might time out.
        """
        access_token = self._get_auth_token()
        headers = {"Authorization": "Bearer {}".format(access_token)}
        return requests.post(self.base_url, json=mutation, headers=headers)

    def delete_learners(self, learners, chunk_size, beginning_idx=0):
        """
        Sets up the Segment GraphQL calls to GDPR-delete users in chunks.

        :param learners: List of learner dicts returned from LMS, should contain all
            we need to retire this learner.
        """
        curr_idx = beginning_idx
        start_idx = 0
        while curr_idx < len(learners):
            start_idx = curr_idx
            end_idx = min(start_idx + chunk_size - 1, len(learners) - 1)
            LOG.info(
                "Attempting Segment deletion with start index %s, \
end index %s for learners (%s, %s) through (%s, %s)...",
                start_idx, end_idx,
                learners[start_idx]['id'], learners[start_idx]['original_username'],
                learners[end_idx]['id'], learners[end_idx]['original_username']
            )

            learner_vals = []
            for idx in range(start_idx, end_idx + 1):
                for id_key in IDENTIFYING_KEYS:
                    learner_vals.append('"{}"'.format(learners[idx][id_key]))

            if len(learner_vals) >= MAXIMUM_USERS_IN_DELETE_REQUEST:
                LOG.error(
                    'Attempting to delete too many user values (%s) at once in bulk request - decrease chunk_size.',
                    len(learner_vals)
                )
                return

            learners_str = '[' + ','.join(learner_vals) + ']'

            mutation = {
                'query': BULK_DELETE_MUTATION.format(self.workspace_slug, learners_str)
            }

            resp = self._call_segment_graphql(mutation)
            resp_json = resp.json()

            try:
                bulk_user_delete_id = resp_json['data'][BULK_DELETE_MUTATION_OPNAME]['id']
                LOG.info('Bulk user deletion queued. Id: {}'.format(bulk_user_delete_id))
            except (TypeError, KeyError):
                LOG.error(u'Error was encountered for learners between start/end indices ({}, {}) : {}'.format(
                    start_idx, end_idx,
                    text_type(resp_json)
                ).encode('utf-8'))
                raise

            curr_idx += chunk_size

    def get_bulk_delete_status(self, bulk_delete_id):
        """
        Queries the status of a previously submitted bulk delete request.

        :param bulk_delete_id: ID returned from a previously-submitted bulk delete request.
        """
        query = {
            'query': BULK_DELETE_STATUS_QUERY.format(bulk_delete_id)
        }

        resp = self._call_segment_graphql(query)
        resp_json = resp.json()
        LOG.info(text_type(resp_json))