# -*- coding: utf-8 -*-
from cabot.cabotapp.alert import AlertPlugin
from cabot.cabotapp.models_plugins import SlackInstance
from cabot.plugin_test_utils import PluginTestCase
from mock import patch, call

from cabot.cabotapp.models import Service
from cabot_alert_slack import models


class TestSlackAlerts(PluginTestCase):
    def setUp(self):
        super(TestSlackAlerts, self).setUp()

        self.alert = AlertPlugin.objects.get(title=models.SlackAlert.name)
        self.service.alerts.add(self.alert)
        self.service.save()

        self.slack_instance = SlackInstance.objects.create(name='Test Slack Instance',
                                                           server_url='https://slack.com',
                                                           access_token='SOME-TOKEN',
                                                           default_channel_id='default-channel')
        self.service.slack_instance = self.slack_instance
        self.service.slack_channel_id = 'C456'

        self.plugin = models.SlackAlert.objects.get()
        self.user.first_name = 'RENÉE'
        self.user.last_name = '☃'

        # self.user's service key is user_key
        models.SlackAlertUserData.objects.create(user=self.user.profile, slack_user_id_override='U123')

    def test_get_slack_api_for_service(self):
        url, headers, channel_id = models._get_slack_api_for_service(self.service)
        self.assertEqual(url, 'https://slack.com/api/')
        self.assertEqual(headers, {
            'Authorization': 'Bearer SOME-TOKEN',
        })
        self.assertEqual(channel_id, 'C456')

    @patch('cabot_alert_slack.models.requests')
    @patch('cabot_alert_slack.models.SlackAlert._email_to_slack_user_id')
    @patch('cabot_alert_slack.models.SlackAlert._join_channel')
    @patch('cabot_alert_slack.models.SlackAlert._ensure_channel_members')
    @patch('cabot_alert_slack.models.SlackAlert._upload_file')
    def test_passing_to_error(self, upload_file, ensure_members, join_channel, email_to_uid, requests):
        email_to_uid.side_effect = lambda url, headers, email: ('U' + email)
        self.run_checks([(self.es_check, False, False)], Service.PASSING_STATUS)

        join_channel.assert_has_calls([
            call('https://slack.com/api/',
                 {'Authorization': 'Bearer SOME-TOKEN'},
                 'C456'),
        ])
        ensure_members.assert_has_calls([
            call('https://slack.com/api/',
                 {'Authorization': 'Bearer SOME-TOKEN'},
                 'C456',
                 ['U123', 'Udolores@affirm.com']),
        ])
        requests.post.assert_has_calls([
            call('https://slack.com/api/chat.postMessage', headers={'Authorization': 'Bearer SOME-TOKEN'},
                 json={
                     'channel': 'C456',
                     'text': 'Service is ERROR',
                     'blocks': [
                         {'text': {'text': ':red_circle: Service status is ERROR :red_circle:', 'type': 'plain_text'},
                          'type': 'header'},
                         {'text': {'text': '*<http://localhost/check/10104/|ES Metric Check>* - ``', 'type': 'mrkdwn'},
                          'type': 'section'},
                         {
                             'type': 'context',
                             'elements': [{'text': '<@U123> <@Udolores@affirm.com> :point_up:', 'type': 'mrkdwn'}]
                         }
                     ]
                 }),
            call().raise_for_status(),
        ])

    @patch('cabot_alert_slack.models.SlackAlert._email_to_slack_user_id')
    @patch('cabot_alert_slack.models.SlackAlert._post_message')
    def test_passing_to_warning(self, post_message, email_to_uid):
        email_to_uid.side_effect = lambda url, headers, email: ('U' + email)

        self.transition_service_status(Service.PASSING_STATUS, Service.WARNING_STATUS)

        post_message.assert_has_calls([
            call(
                'https://slack.com/api/', {'Authorization': 'Bearer SOME-TOKEN'},
                text='Service is WARNING',
                blocks=[
                    {
                        'type': 'header',
                        'text': {
                            'type': 'plain_text',
                            'text': ':large_yellow_circle: Service status is WARNING :large_yellow_circle:',
                        }
                    },
                    # no failing checks in this test case
                    # no @mentions for warning-level
                ],
                channel_id='C456')
        ])

    @patch('cabot_alert_slack.models.SlackAlert._email_to_slack_user_id')
    @patch('cabot_alert_slack.models.SlackAlert._post_message')
    def test_error_to_acked(self, post_message, email_to_uid):
        email_to_uid.side_effect = lambda url, headers, email: ('U' + email)

        self.transition_service_status(Service.ERROR_STATUS, Service.ACKED_STATUS)

        post_message.assert_has_calls([
            call(
                'https://slack.com/api/', {'Authorization': 'Bearer SOME-TOKEN'},
                text='Service is ACKED',
                blocks=[
                    {
                        'type': 'header',
                        'text': {
                            'type': 'plain_text',
                            'text': ':zipper_mouth_face: Service status is ACKED :zipper_mouth_face:',
                        }
                    },
                    # no failing checks in this test case
                    # no @mentions for acked
                ],
                channel_id='C456')
        ])

    @patch('cabot_alert_slack.models.SlackAlert._post_message')
    def test_acked_to_acked(self, send_alert):
        self.transition_service_status(Service.ACKED_STATUS, Service.ACKED_STATUS)
        self.assertFalse(send_alert.called)

    @patch('cabot_alert_slack.models.SlackAlert._post_message')
    def test_passing_to_acked(self, send_alert):
        self.transition_service_status(Service.PASSING_STATUS, Service.ACKED_STATUS)
        self.assertFalse(send_alert.called)

    @patch('cabot_alert_slack.models.SlackAlert._post_message')
    def test_acked_to_passing(self, send_alert):
        self.transition_service_status(Service.ACKED_STATUS, Service.PASSING_STATUS)
        self.assertFalse(send_alert.called)