from django.core.exceptions import ValidationError
from django.core.urlresolvers import reverse
from django.conf import settings
from django.db import models
from urlparse import urljoin
from cabot.cabotapp.alert import AlertPlugin, AlertPluginUserData
from cabot.cabotapp.utils import build_absolute_url
from cabot.metricsapp.models import MetricsStatusCheckBase

import requests
import logging

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from cabot.cabotapp.models import Service
    from typing import Any, Dict, List, Iterable, Set, Tuple

logger = logging.getLogger(__name__)

# if a user's ID matches this, they won't be @mentioned or called out
# this is useful for dummy users (PagerDuty, mailing list users, etc.)
IGNORE_USER_ID = 'ignore'

EMOJIS = {
    'WARNING': ":large_yellow_circle:",
    'ERROR': ":red_circle:",
    'CRITICAL': ":alert:",
    'PASSING': ":large_green_circle:",
    'ACKED': ":zipper_mouth_face:",
}


class SlackAPIError(Exception):
    def __init__(self, error_type, errors):
        # type: (str, List[str]) -> None
        self.error_type = error_type
        self.errors = errors

    def __str__(self):
        s = 'Slack API returned not ok, error type: ' + self.error_type
        if self.errors:
            s += ', errors: ' + repr(self.errors)
        return s


def _check_response(response, check_ok=True):
    # type: (requests.Response, bool) -> None
    """Raise for status, but include the full response in the exception to show error messages"""
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise requests.HTTPError(e.message + ', response body: ' + response.text, response=response)

    if check_ok and not response.json().get('ok'):
        raise SlackAPIError(response.json().get('error', '<error field missing>'), response.json().get('errors'))


def _get_slack_api_for_service(service):
    """
    :param service: the service to pull from (to get Slack instance, channel id)
    :return: a tuple of (api_endpoint_url, http_headers, channel_id)
    """
    if service.slack_instance is not None:
        server_url = service.slack_instance.server_url
        access_token = service.slack_instance.access_token
        channel_id = service.slack_instance.default_channel_id
    else:
        raise RuntimeError('Slack instance not set.')

    if service.slack_channel_id:
        channel_id = service.slack_channel_id
    if not channel_id:
        raise RuntimeError('Slack channel ID not set.')

    api_url = urljoin(server_url, 'api/')
    headers = {
        'Authorization': 'Bearer {}'.format(access_token),
    }
    return api_url, headers, channel_id


class SlackAlert(AlertPlugin):
    name = "Slack"
    author = "Alec Lofquist"

    def _email_to_slack_user_id(self, url, headers, email):
        # type: (str, Dict[str, str], str) -> str
        """
        Look up a Slack user ID by email. Raises SlackAPIError if the user can't be found.
        :param url: slack api endpoint
        :param headers: auth headers
        :param email: email to look up
        :return: slack user ID
        """
        response = requests.get(urljoin(url, 'users.lookupByEmail'), headers=headers, params={
            'email': email
        })
        _check_response(response)
        return response.json()['user']['id']

    def _cabot_user_to_slack_user_id(self, url, headers, user):
        # type: (str, Dict[str, str], User) -> str
        """
        Map a Cabot user to their Slack user ID. Raises SlackAPIError if the user's email can't be found in Slack.
        :param url: slack api url
        :param headers: auth headers
        :param user: Cabot user
        :return: slack user ID
        """
        # check for user ID override
        # note some users might not have SlackAlertUserData objects created yet, so this may be empty list
        for slack_data in SlackAlertUserData.objects.filter(user__user=user):
            if slack_data.slack_user_id_override:
                return slack_data.slack_user_id_override
        return self._email_to_slack_user_id(url, headers, user.email)

    def _get_channel_members(self, url, headers, channel_id):
        # type: (str, Dict[str, str], str) -> Set[str]
        """
        Get a list of user IDs who are in a channel.
        :param url: slack api url
        :param headers: auth headers
        :param channel_id: slack channel ID to get users from
        :return: set of user ID strings
        """

        params = {
            'channel': channel_id,
        }

        user_ids = []
        while True:
            response = requests.get(urljoin(url, 'conversations.members'), headers=headers, params=params)
            _check_response(response)
            user_ids += response.json()['members']

            # handle pagination
            params['cursor'] = response.json().get('response_metadata', {}).get('next_cursor')
            if not params['cursor']:
                break
        return set(user_ids)

    def _join_channel(self, url, headers, channel_id):
        # type: (str, Dict[str, str], str) -> None
        # if the bot is already in channel, still succeeds
        response = requests.post(urljoin(url, 'conversations.join'), headers=headers, json={
            'channel': channel_id,
        })
        _check_response(response)

    def _ensure_channel_members(self, url, headers, channel_id, user_ids):
        # type: (str, Dict[str, str], str, List[str]) -> None
        """
        Adds the given list of user IDs to the given channel_id.
        Raises a SlackAPIError if some users could not be invited to the channel.
        :param url: slack api endpoint
        :param headers: HTTP headers (w/ access token)
        :param channel_id: channel ID to add users to
        :param user_ids: list of slack user IDs to ensure are invited to the channel
        :return: None
        """
        if len(user_ids) == 0:
            return

        user_ids_in_channel = self._get_channel_members(url, headers, channel_id)
        missing_user_ids = list(set(user_ids) - user_ids_in_channel)

        response = requests.post(urljoin(url, 'conversations.invite'), headers=headers, json={
            'channel': channel_id,
            'users': ','.join(missing_user_ids)
        })
        _check_response(response)

    def _upload_file(self, url, headers, file_name, file_data, channel_id, thread_ts, timeout_seconds=30):
        # type: (str, Dict[str, str], str, bytes, str, str, int) -> Dict[str, str]
        """
        Upload a file to slack.
        :param url: slack api endpoint
        :param headers: HTTP headers (w/ access token)
        :param file_name: file name
        :param file_data: file contents (bytes)
        :param channel_id: channel ID to share with
        :param thread_ts: parent thread id to post the file as a reply to
        :param timeout_seconds: timeout for uploading all files (default 30s)
        :return: a slack file object (dict from json)
        """
        response = requests.post(
            urljoin(url, 'files.upload'),
            data={'filename': file_name, 'channels': channel_id, 'thread_ts': thread_ts},
            files={'file': file_data},
            headers=headers,
            timeout=timeout_seconds,
        )
        _check_response(response)
        return response.json()['file']

    def _post_message(self, url, headers, text, blocks, channel_id):
        # type: (str, Dict[str, str], str, List[Any], str) -> str
        """
        Post a message to a channel. Note the Cabot integration should be in the channel for this to work.
        :param url: slack api endpoint
        :param headers: auth headers
        :param text: message text (used as fallback/notification text if blocks is specified)
        :param blocks: slack block layout data
        :param channel_id: channel to post to
        :return: the post's "ts" value (used for replies)
        """
        response = requests.post(urljoin(url, 'chat.postMessage'), headers=headers, json={
            'channel': channel_id,
            'text': text,  # this shows in notifications when using blocks
            'blocks': blocks,
        })
        _check_response(response)
        return response.json()['ts']

    def send_alert(self, service, users, duty_officers):
        # type: (Service, Iterable[User], Iterable[User]) -> None
        include_mentions = True
        users = list(users) + list(duty_officers)

        current_status = service.overall_status
        old_status = service.old_overall_status

        if current_status == service.WARNING_STATUS:
            # Don't alert at all for WARNING
            include_mentions = False
        if current_status == service.ERROR_STATUS:
            if old_status == service.ERROR_STATUS:
                # Don't alert repeatedly for ERROR
                include_mentions = False
        if current_status == service.PASSING_STATUS:
            if old_status == service.ACKED_STATUS:
                # Don't message repeatedly for new successes after ACKED failures
                return
            if old_status == service.WARNING_STATUS:
                # Don't alert for recovery from WARNING status
                include_mentions = False
        if current_status == service.ACKED_STATUS:
            if old_status == service.ACKED_STATUS:
                # Don't message repeatedly for ACKED status
                return
            if old_status == service.PASSING_STATUS:
                # Don't message for acked failures even it started passing
                return
            # Don't @mention when transitioning into the ACKED status
            include_mentions = False

        url, headers, channel_id = _get_slack_api_for_service(service)
        failing_checks = list(service.all_failing_checks())

        # ensure cabot is in channel
        try:
            self._join_channel(url, headers, channel_id)
        except (requests.HTTPError, SlackAPIError) as e:
            if isinstance(e, SlackAPIError) and e.error_type == 'method_not_supported_for_channel_type':
                # private channel; someone must add the integration manually
                pass
            else:
                logger.warning('Could not join channel %s: %s.', channel_id, e)

        # map cabot users to slack user IDs
        user_ids = []  # type: List[str]
        # list of users that aren't found on slack and aren't ignored
        missing_users = []  # type: List[User]
        for user in users:
            try:
                user_id = self._cabot_user_to_slack_user_id(url, headers, user)
                if user_id != IGNORE_USER_ID:
                    user_ids.append(user_id)
            except (requests.HTTPError, SlackAPIError) as e:
                missing_users.append(user)
                if not (isinstance(e, SlackAPIError) and e.error_type == 'users_not_found'):
                    logger.exception('Failed to find Slack user for Cabot user %s, got unexpected error %s.',
                                     user, e.error_type)

        # ensure users are in channel
        try:
            self._ensure_channel_members(url, headers, channel_id, user_ids)
        except (SlackAPIError, requests.HTTPError) as e:
            logger.exception('Failed to add users to channel %s: %s', channel_id, e)

        blocks = [
            {
                'type': 'header',
                'text': {
                    'type': 'plain_text',
                    'text': '{emoji} {service} status is {status} {emoji}'.format(service=service.name,
                                                                                  status=current_status.upper(),
                                                                                  emoji=EMOJIS.get(current_status, '')),
                }
            }
        ]
        for check in failing_checks:
            last_result = check.last_result()
            error = last_result.error if last_result else None  # type: Optional[str]
            check_link = build_absolute_url(reverse('check', kwargs={'pk': check.pk}))

            status_link = check.get_status_link()
            status_text = ''
            if isinstance(check, MetricsStatusCheckBase):
                status_text = 'Grafana'
            elif check.check_category == 'Jenkins Check':
                status_link = '{jenkins}job/{check_name}/{job_number}/console'.format(
                    jenkins=urljoin(settings.JENKINS_API, '/'),
                    check_name=check.name,
                    job_number=last_result.job_number if last_result else None,
                )
                status_text = 'Jenkins'

            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*<{link}|{name}>* - `{error}`".format(link=check_link,
                                                                   name=check.name.replace('>', '\\>'),
                                                                   error=error.replace('`', '\\`') if error else '')
                },
            })

            # add an accessory button that links to the check's link if present (i.e. Grafana)
            if status_link:
                blocks[-1]["accessory"] = {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": status_text,
                        "emoji": False,
                    },
                    "url": status_link,
                    "action_id": "button-status"
                }

        # add @mentions
        if include_mentions:
            if user_ids:
                blocks.append({
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": " ".join("<@{}>".format(user_id) for user_id in user_ids) + " :point_up:"
                        }
                    ]
                })
            if missing_users:
                missing_users_data = []  # type: List[Tuple[str, str]]
                for user in missing_users:
                    name = user.email or user.username
                    if user.first_name and user.last_name:
                        name += u' ({} {})'.format(user.first_name, user.last_name)

                    profile_link = build_absolute_url(reverse('update-alert-user-data',
                                                              kwargs={'pk': user.pk, 'alerttype': 'Slack Plugin'}))
                    missing_users_data.append((name, profile_link))

                missing_users_str = ', '.join('{name} (<{link}|profile>)'.format(link=link, name=name)
                                              for name, link in missing_users_data)
                blocks.append({
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "Could not find Slack account for some users: {missing_users}.\n"
                                    "Please ensure they have a Slack account. "
                                    "If their Slack email doesn't match their Cabot email, set a user ID override in "
                                    "their Cabot profile, or enter an ID of '{ignore}' to silence this warning.".format(
                                        missing_users=missing_users_str, ignore=IGNORE_USER_ID)
                        }
                    ]
                })

        try:
            ts = self._post_message(url, headers,
                                    text='{} is {}'.format(service.name, service.overall_status),
                                    blocks=blocks,
                                    channel_id=channel_id)
        except (SlackAPIError, requests.HTTPError) as e:
            logger.exception('Error posting message to Slack channel %s: %s', channel_id, e)
            raise

        # Upload images for first 5 failing checks as replies to the main post
        try:
            for check in failing_checks[:5]:
                image = check.get_status_image()
                if image is not None:
                    self._upload_file(url, headers, '{}.png'.format(check.name), image, channel_id, ts)
        except (requests.HTTPError, SlackAPIError) as e:
            # continue anyway, just don't put any images in the message
            logger.exception('Failed to get/upload images to channel ID %s: %s', channel_id, e)


def validate_slack_user_id(slack_id):
    if not (slack_id.startswith('U') or slack_id.startswith('W')):
        raise ValidationError('Slack user ID should start with a U or W')


class SlackAlertUserData(AlertPluginUserData):
    '''
    This provides a way to specify a slack user ID for a user.
    Each object corresponds to a User
    '''
    name = "Slack Plugin"
    slack_user_id_override = models.CharField(max_length=50, blank=True, validators=[validate_slack_user_id],
                                              help_text="Optional override for your SLack user ID. "
                                                        "You only need to set this if your Cabot email does not "
                                                        "match your Slack email. "
                                                        "Enter '" + IGNORE_USER_ID + "' to disable @mentions.")

    def is_configured(self):
        return True
