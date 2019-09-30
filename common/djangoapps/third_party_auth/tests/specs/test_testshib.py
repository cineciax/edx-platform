"""
Third_party_auth integration tests using a mock version of the TestShib provider
"""
from __future__ import absolute_import

import datetime
import json
import logging
import os
import unittest
from unittest import skip

import ddt
import httpretty
from django.contrib import auth
from freezegun import freeze_time
from mock import MagicMock, patch
from social_core import actions
from social_django import views as social_views
from social_django.models import UserSocialAuth
from testfixtures import LogCapture

from enterprise.models import EnterpriseCustomerIdentityProvider, EnterpriseCustomerUser
from openedx.core.djangoapps.user_authn.views.deprecated import signin_user
from openedx.core.djangoapps.user_authn.views.login import login_user
from openedx.core.djangoapps.user_api.accounts.settings_views import account_settings_context
from openedx.features.enterprise_support.tests.factories import EnterpriseCustomerFactory
from third_party_auth import pipeline
from third_party_auth.saml import SapSuccessFactorsIdentityProvider, log as saml_log
from third_party_auth.tasks import fetch_saml_metadata
from third_party_auth.tests import testutil, utils

from .base import IntegrationTestMixin

TESTSHIB_ENTITY_ID = 'https://idp.testshib.org/idp/shibboleth'
TESTSHIB_METADATA_URL = 'https://mock.testshib.org/metadata/testshib-providers.xml'
TESTSHIB_METADATA_URL_WITH_CACHE_DURATION = 'https://mock.testshib.org/metadata/testshib-providers-cache.xml'
TESTSHIB_SSO_URL = 'https://idp.testshib.org/idp/profile/SAML2/Redirect/SSO'


class SamlIntegrationTestUtilities(object):
    """
    Class contains methods particular to SAML integration testing so that they
    can be separated out from the actual test methods.
    """
    PROVIDER_ID = "saml-testshib"
    PROVIDER_NAME = "TestShib"
    PROVIDER_BACKEND = "tpa-saml"
    PROVIDER_IDP_SLUG = "testshib"

    USER_EMAIL = "myself@testshib.org"
    USER_NAME = "Me Myself And I"
    USER_USERNAME = "myself"

    def setUp(self):
        super(SamlIntegrationTestUtilities, self).setUp()
        self.enable_saml(
            private_key=self._get_private_key(),
            public_key=self._get_public_key(),
            entity_id="https://saml.example.none",
        )
        # Mock out HTTP requests that may be made to TestShib:
        httpretty.enable()
        httpretty.reset()
        self.addCleanup(httpretty.reset)
        self.addCleanup(httpretty.disable)

        def metadata_callback(_request, _uri, headers):
            """ Return a cached copy of TestShib's metadata by reading it from disk """
            return (200, headers, self.read_data_file('testshib_metadata.xml'))

        httpretty.register_uri(httpretty.GET, TESTSHIB_METADATA_URL, content_type='text/xml', body=metadata_callback)

        def cache_duration_metadata_callback(_request, _uri, headers):
            """Return a cached copy of TestShib's metadata with a cacheDuration attribute"""
            return (200, headers, self.read_data_file('testshib_metadata_with_cache_duration.xml'))

        httpretty.register_uri(
            httpretty.GET,
            TESTSHIB_METADATA_URL_WITH_CACHE_DURATION,
            content_type='text/xml',
            body=cache_duration_metadata_callback
        )

        # Configure the SAML library to use the same request ID for every request.
        # Doing this and freezing the time allows us to play back recorded request/response pairs
        uid_patch = patch('onelogin.saml2.utils.OneLogin_Saml2_Utils.generate_unique_id', return_value='TESTID')
        uid_patch.start()
        self.addCleanup(uid_patch.stop)
        self._freeze_time(timestamp=1434326820)  # This is the time when the saved request/response was recorded.

    def _freeze_time(self, timestamp):
        """ Mock the current time for SAML, so we can replay canned requests/responses """
        now_patch = patch('onelogin.saml2.utils.OneLogin_Saml2_Utils.now', return_value=timestamp)
        now_patch.start()
        self.addCleanup(now_patch.stop)

    def _configure_testshib_provider(self, **kwargs):
        """ Enable and configure the TestShib SAML IdP as a third_party_auth provider """
        fetch_metadata = kwargs.pop('fetch_metadata', True)
        assert_metadata_updates = kwargs.pop('assert_metadata_updates', True)
        kwargs.setdefault('name', self.PROVIDER_NAME)
        kwargs.setdefault('enabled', True)
        kwargs.setdefault('visible', True)
        kwargs.setdefault("backend_name", "tpa-saml")
        kwargs.setdefault('slug', self.PROVIDER_IDP_SLUG)
        kwargs.setdefault('entity_id', TESTSHIB_ENTITY_ID)
        kwargs.setdefault('metadata_source', TESTSHIB_METADATA_URL)
        kwargs.setdefault('icon_class', 'fa-university')
        kwargs.setdefault('attr_email', 'urn:oid:1.3.6.1.4.1.5923.1.1.1.6')  # eduPersonPrincipalName
        kwargs.setdefault('max_session_length', None)
        saml_provider = self.configure_saml_provider(**kwargs)  # pylint: disable=no-member

        if fetch_metadata:
            self.assertTrue(httpretty.is_enabled())
            num_total, num_skipped, num_attempted, num_updated, num_failed, failure_messages = fetch_saml_metadata()
            if assert_metadata_updates:
                self.assertEqual(num_total, 1)
                self.assertEqual(num_skipped, 0)
                self.assertEqual(num_attempted, 1)
                self.assertEqual(num_updated, 1)
                self.assertEqual(num_failed, 0)
                self.assertEqual(len(failure_messages), 0)
        return saml_provider

    def do_provider_login(self, provider_redirect_url):
        """ Mocked: the user logs in to TestShib and then gets redirected back """
        # The SAML provider (TestShib) will authenticate the user, then get the browser to POST a response:
        self.assertTrue(provider_redirect_url.startswith(TESTSHIB_SSO_URL))

        saml_response_xml = utils.read_and_pre_process_xml(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'testshib_saml_response.xml')
        )

        return self.client.post(
            self.complete_url,
            content_type='application/x-www-form-urlencoded',
            data=utils.prepare_saml_response_from_xml(saml_response_xml),
        )


@ddt.ddt
@unittest.skipUnless(testutil.AUTH_FEATURE_ENABLED, testutil.AUTH_FEATURES_KEY + ' not enabled')
class TestShibIntegrationTest(SamlIntegrationTestUtilities, IntegrationTestMixin, testutil.SAMLTestCase):
    """
    TestShib provider Integration Test, to test SAML functionality
    """

    TOKEN_RESPONSE_DATA = {
        'access_token': 'access_token_value',
        'expires_in': 'expires_in_value',
    }
    USER_RESPONSE_DATA = {
        'lastName': 'lastName_value',
        'id': 'id_value',
        'firstName': 'firstName_value',
        'idp_name': 'testshib',
        'attributes': {u'urn:oid:0.9.2342.19200300.100.1.1': [u'myself']}
    }

    def test_full_pipeline_succeeds_for_unlinking_testshib_account(self):

        # First, create, the request and strategy that store pipeline state,
        # configure the backend, and mock out wire traffic.
        self.provider = self._configure_testshib_provider()
        request, strategy = self.get_request_and_strategy(
            auth_entry=pipeline.AUTH_ENTRY_LOGIN, redirect_uri='social:complete')
        request.backend.auth_complete = MagicMock(return_value=self.fake_auth_complete(strategy))
        user = self.create_user_models_for_existing_account(
            strategy, 'user@example.com', 'password', self.get_username())
        self.assert_social_auth_exists_for_user(user, strategy)

        request.user = user

        # We're already logged in, so simulate that the cookie is set correctly
        self.set_logged_in_cookies(request)

        # linking a learner with enterprise customer.
        enterprise_customer = EnterpriseCustomerFactory()
        assert EnterpriseCustomerUser.objects.count() == 0, "Precondition check: no link records should exist"
        EnterpriseCustomerUser.objects.link_user(enterprise_customer, user.email)
        self.assertTrue(
            EnterpriseCustomerUser.objects.filter(enterprise_customer=enterprise_customer, user_id=user.id).count() == 1
        )
        EnterpriseCustomerIdentityProvider.objects.get_or_create(enterprise_customer=enterprise_customer,
                                                                 provider_id=self.provider.provider_id)

        # Instrument the pipeline to get to the dashboard with the full expected state.
        self.client.get(
            pipeline.get_login_url(self.provider.provider_id, pipeline.AUTH_ENTRY_LOGIN))
        actions.do_complete(request.backend, social_views._do_login,  # pylint: disable=protected-access
                            request=request)

        with self._patch_edxmako_current_request(strategy.request):
            signin_user(strategy.request)
            login_user(strategy.request)
            actions.do_complete(request.backend, social_views._do_login, user=user,  # pylint: disable=protected-access
                                request=request)

        # First we expect that we're in the linked state, with a backend entry.
        self.assert_account_settings_context_looks_correct(account_settings_context(request), linked=True)
        self.assert_social_auth_exists_for_user(request.user, strategy)

        # Fire off the disconnect pipeline without the user information.
        actions.do_disconnect(
            request.backend,
            None,
            None,
            redirect_field_name=auth.REDIRECT_FIELD_NAME,
            request=request
        )
        self.assertFalse(
            EnterpriseCustomerUser.objects.filter(enterprise_customer=enterprise_customer, user_id=user.id).count() == 0
        )

        # Fire off the disconnect pipeline to unlink.
        self.assert_redirect_after_pipeline_completes(
            actions.do_disconnect(
                request.backend,
                user,
                None,
                redirect_field_name=auth.REDIRECT_FIELD_NAME,
                request=request
            )
        )
        # Now we expect to be in the unlinked state, with no backend entry.
        self.assert_account_settings_context_looks_correct(account_settings_context(request), linked=False)
        self.assert_social_auth_does_not_exist_for_user(user, strategy)
        self.assertTrue(
            EnterpriseCustomerUser.objects.filter(enterprise_customer=enterprise_customer, user_id=user.id).count() == 0
        )

    def get_response_data(self):
        """Gets dict (string -> object) of merged data about the user."""
        response_data = dict(self.TOKEN_RESPONSE_DATA)
        response_data.update(self.USER_RESPONSE_DATA)
        return response_data

    def get_username(self):
        response_data = self.get_response_data()
        return response_data.get('idp_name')

    def test_login_before_metadata_fetched(self):
        self._configure_testshib_provider(fetch_metadata=False)
        # The user goes to the login page, and sees a button to login with TestShib:
        testshib_login_url = self._check_login_page()
        # The user clicks on the TestShib button:
        try_login_response = self.client.get(testshib_login_url)
        # The user should be redirected to back to the login page:
        self.assertEqual(try_login_response.status_code, 302)
        self.assertEqual(try_login_response['Location'], self.login_page_url)
        # When loading the login page, the user will see an error message:
        response = self.client.get(self.login_page_url)
        self.assertContains(response, 'Authentication with TestShib is currently unavailable.')

    def test_login(self):
        """ Configure TestShib before running the login test """
        self._configure_testshib_provider()
        self._test_login()

    def test_register(self):
        """ Configure TestShib before running the register test """
        self._configure_testshib_provider()
        self._test_register()

    def test_login_records_attributes(self):
        """
        Test that attributes sent by a SAML provider are stored in the UserSocialAuth table.
        """
        self.test_login()
        record = UserSocialAuth.objects.get(
            user=self.user, provider=self.PROVIDER_BACKEND, uid__startswith=self.PROVIDER_IDP_SLUG
        )
        attributes = record.extra_data
        self.assertEqual(
            attributes.get("urn:oid:1.3.6.1.4.1.5923.1.1.1.9"), ["Member@testshib.org", "Staff@testshib.org"]
        )
        self.assertEqual(attributes.get("urn:oid:2.5.4.3"), ["Me Myself And I"])
        self.assertEqual(attributes.get("urn:oid:0.9.2342.19200300.100.1.1"), ["myself"])
        self.assertEqual(attributes.get("urn:oid:2.5.4.20"), ["555-5555"])  # Phone number

    @ddt.data(True, False)
    def test_debug_mode_login(self, debug_mode_enabled):
        """ Test SAML login logs with debug mode enabled or not """
        self._configure_testshib_provider(debug_mode=debug_mode_enabled)
        with patch.object(saml_log, 'info') as mock_log:
            self._test_login()
        if debug_mode_enabled:
            # We expect that test_login() does two full logins, and each attempt generates two
            # logs - one for the request and one for the response
            self.assertEqual(mock_log.call_count, 4)

            (msg, action_type, idp_name, xml), _kwargs = mock_log.call_args_list[0]
            self.assertTrue(msg.startswith(u"SAML login %s"))
            self.assertEqual(action_type, "request")
            self.assertEqual(idp_name, self.PROVIDER_IDP_SLUG)
            self.assertIn('<samlp:AuthnRequest', xml)

            (msg, action_type, idp_name, xml), _kwargs = mock_log.call_args_list[1]
            self.assertTrue(msg.startswith(u"SAML login %s"))
            self.assertEqual(action_type, "response")
            self.assertEqual(idp_name, self.PROVIDER_IDP_SLUG)
            self.assertIn('<saml2p:Response', xml)
        else:
            self.assertFalse(mock_log.called)

    def test_configure_testshib_provider_with_cache_duration(self):
        """ Enable and configure the TestShib SAML IdP as a third_party_auth provider """
        kwargs = {}
        kwargs.setdefault('name', self.PROVIDER_NAME)
        kwargs.setdefault('enabled', True)
        kwargs.setdefault('visible', True)
        kwargs.setdefault('slug', self.PROVIDER_IDP_SLUG)
        kwargs.setdefault('entity_id', TESTSHIB_ENTITY_ID)
        kwargs.setdefault('metadata_source', TESTSHIB_METADATA_URL_WITH_CACHE_DURATION)
        kwargs.setdefault('icon_class', 'fa-university')
        kwargs.setdefault('attr_email', 'urn:oid:1.3.6.1.4.1.5923.1.1.1.6')  # eduPersonPrincipalName
        self.configure_saml_provider(**kwargs)
        self.assertTrue(httpretty.is_enabled())
        num_total, num_skipped, num_attempted, num_updated, num_failed, failure_messages = fetch_saml_metadata()
        self.assertEqual(num_total, 1)
        self.assertEqual(num_skipped, 0)
        self.assertEqual(num_attempted, 1)
        self.assertEqual(num_updated, 1)
        self.assertEqual(num_failed, 0)
        self.assertEqual(len(failure_messages), 0)

    def test_login_with_testshib_provider_short_session_length(self):
        """
        Test that when we have a TPA provider which as an explicit maximum
        session length set, waiting for longer than that between requests
        results in us being logged out.
        """
        # Configure the provider with a 10-second timeout
        self._configure_testshib_provider(max_session_length=10)

        now = datetime.datetime.utcnow()
        with freeze_time(now):
            # Test the login flow, adding the user in the process
            self._test_login()

        # Wait 30 seconds; longer than the manually-set 10-second timeout
        later = now + datetime.timedelta(seconds=30)
        with freeze_time(later):
            # Test returning as a logged in user; this method verifies that we're logged out first.
            self._test_return_login(previous_session_timed_out=True)


@unittest.skipUnless(testutil.AUTH_FEATURE_ENABLED, testutil.AUTH_FEATURES_KEY + ' not enabled')
class SuccessFactorsIntegrationTest(SamlIntegrationTestUtilities, IntegrationTestMixin, testutil.SAMLTestCase):
    """
    Test basic SAML capability using the TestShib details, and then check that we're able
    to make the proper calls using the SAP SuccessFactors API.
    """

    # Note that these details are different than those that will be provided by the SAML
    # assertion metadata. Rather, they will be fetched from the mocked SAPSuccessFactors API.
    USER_EMAIL = "john@smith.com"
    USER_NAME = "John Smith"
    USER_USERNAME = "jsmith"

    def setUp(self):
        """
        Mock out HTTP calls to various endpoints using httpretty.
        """
        super(SuccessFactorsIntegrationTest, self).setUp()

        # Mock the call to the SAP SuccessFactors assertion endpoint
        SAPSF_ASSERTION_URL = 'http://successfactors.com/oauth/idp'

        def assertion_callback(_request, _uri, headers):
            """
            Return a fake assertion after checking that the input is what we expect.
            """
            self.assertIn(b'private_key=fake_private_key_here', _request.body)
            self.assertIn(b'user_id=myself', _request.body)
            self.assertIn(b'token_url=http%3A%2F%2Fsuccessfactors.com%2Foauth%2Ftoken', _request.body)
            self.assertIn(b'client_id=TatVotSEiCMteSNWtSOnLanCtBGwNhGB', _request.body)
            return (200, headers, 'fake_saml_assertion')

        httpretty.register_uri(httpretty.POST, SAPSF_ASSERTION_URL, content_type='text/plain', body=assertion_callback)

        SAPSF_BAD_ASSERTION_URL = 'http://successfactors.com/oauth-fake/idp'

        def bad_callback(_request, _uri, headers):
            """
            Return a 404 error when someone tries to call the URL.
            """
            return (404, headers, 'NOT AN ASSERTION')

        httpretty.register_uri(httpretty.POST, SAPSF_BAD_ASSERTION_URL, content_type='text/plain', body=bad_callback)

        # Mock the call to the SAP SuccessFactors token endpoint
        SAPSF_TOKEN_URL = 'http://successfactors.com/oauth/token'

        def token_callback(_request, _uri, headers):
            """
            Return a fake assertion after checking that the input is what we expect.
            """
            self.assertIn(b'assertion=fake_saml_assertion', _request.body)
            self.assertIn(b'company_id=NCC1701D', _request.body)
            self.assertIn(b'grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Asaml2-bearer', _request.body)
            self.assertIn(b'client_id=TatVotSEiCMteSNWtSOnLanCtBGwNhGB', _request.body)
            return (200, headers, '{"access_token": "faketoken"}')

        httpretty.register_uri(httpretty.POST, SAPSF_TOKEN_URL, content_type='application/json', body=token_callback)

        # Mock the call to the SAP SuccessFactors OData user endpoint
        ODATA_USER_URL = (
            'http://api.successfactors.com/odata/v2/User(userId=\'myself\')'
            '?$select=username,firstName,lastName,defaultFullName,email'
        )

        def user_callback(request, _uri, headers):
            auth_header = request.headers.get('Authorization')
            self.assertEqual(auth_header, 'Bearer faketoken')
            return (
                200,
                headers,
                json.dumps({
                    'd': {
                        'username': 'jsmith',
                        'firstName': 'John',
                        'lastName': 'Smith',
                        'defaultFullName': 'John Smith',
                        'email': 'john@smith.com',
                        'country': 'Australia',
                    }
                })
            )

        httpretty.register_uri(httpretty.GET, ODATA_USER_URL, content_type='application/json', body=user_callback)

    def _mock_odata_api_for_error(self, odata_api_root_url, username):
        """
        Mock an error response when calling the OData API for user details.
        """

        def callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Return a 500 error when someone tries to call the URL.
            """
            headers['CorrelationId'] = 'aefd38b7-c92c-445a-8c7a-487a3f0c7a9d'
            headers['RequestNo'] = '[787177]'  # This is the format SAPSF returns for the transaction request number
            return 500, headers, 'Failure!'

        fields = ','.join(SapSuccessFactorsIdentityProvider.default_field_mapping.copy())
        url = '{root_url}User(userId=\'{user_id}\')?$select={fields}'.format(
            root_url=odata_api_root_url,
            user_id=username,
            fields=fields,
        )
        httpretty.register_uri(httpretty.GET, url, body=callback, content_type='application/json')
        return url

    def test_register_insufficient_sapsf_metadata(self):
        """
        Configure the provider such that it doesn't have enough details to contact the SAP
        SuccessFactors API, and test that it falls back to the data it receives from the SAML assertion.
        """
        self._configure_testshib_provider(
            identity_provider_type='sap_success_factors',
            metadata_source=TESTSHIB_METADATA_URL,
            other_settings='{"key_i_dont_need":"value_i_also_dont_need"}',
        )
        # Because we're getting details from the assertion, fall back to the initial set of details.
        self.USER_EMAIL = "myself@testshib.org"
        self.USER_NAME = "Me Myself And I"
        self.USER_USERNAME = "myself"
        self._test_register()

    @patch.dict('django.conf.settings.REGISTRATION_EXTRA_FIELDS', country='optional')
    def test_register_sapsf_metadata_present(self):
        """
        Configure the provider such that it can talk to a mocked-out version of the SAP SuccessFactors
        API, and ensure that the data it gets that way gets passed to the registration form.

        Check that value mappings overrides work in cases where we override a value other than
        what we're looking for, and when an empty override is provided (expected behavior is that
        existing value maps will be left alone).
        """
        expected_country = 'AU'
        provider_settings = {
            'sapsf_oauth_root_url': 'http://successfactors.com/oauth/',
            'sapsf_private_key': 'fake_private_key_here',
            'odata_api_root_url': 'http://api.successfactors.com/odata/v2/',
            'odata_company_id': 'NCC1701D',
            'odata_client_id': 'TatVotSEiCMteSNWtSOnLanCtBGwNhGB',
        }

        self._configure_testshib_provider(
            identity_provider_type='sap_success_factors',
            metadata_source=TESTSHIB_METADATA_URL,
            other_settings=json.dumps(provider_settings)
        )
        self._test_register(country=expected_country)

    def test_register_sapsf_with_value_default(self):
        """
        Configure the provider such that it can talk to a mocked-out version of the SAP SuccessFactors
        API, and ensure that the data it gets that way gets passed to the registration form.

        Check that value mappings overrides work in cases where we override a value other than
        what we're looking for, and when an empty override is provided it should use the default value
        provided by the configuration.
        """
        # Mock the call to the SAP SuccessFactors OData user endpoint
        ODATA_USER_URL = (
            'http://api.successfactors.com/odata/v2/User(userId=\'myself\')'
            '?$select=username,firstName,country,lastName,defaultFullName,email'
        )

        def user_callback(request, _uri, headers):
            auth_header = request.headers.get('Authorization')
            self.assertEqual(auth_header, 'Bearer faketoken')
            return (
                200,
                headers,
                json.dumps({
                    'd': {
                        'username': 'jsmith',
                        'firstName': 'John',
                        'lastName': 'Smith',
                        'defaultFullName': 'John Smith',
                        'country': 'Australia'
                    }
                })
            )

        httpretty.register_uri(httpretty.GET, ODATA_USER_URL, content_type='application/json', body=user_callback)

        provider_settings = {
            'sapsf_oauth_root_url': 'http://successfactors.com/oauth/',
            'sapsf_private_key': 'fake_private_key_here',
            'odata_api_root_url': 'http://api.successfactors.com/odata/v2/',
            'odata_company_id': 'NCC1701D',
            'odata_client_id': 'TatVotSEiCMteSNWtSOnLanCtBGwNhGB',
        }

        self._configure_testshib_provider(
            identity_provider_type='sap_success_factors',
            metadata_source=TESTSHIB_METADATA_URL,
            other_settings=json.dumps(provider_settings),
            default_email='default@testshib.org'
        )
        self.USER_EMAIL = 'default@testshib.org'
        self._test_register()

    @patch.dict('django.conf.settings.REGISTRATION_EXTRA_FIELDS', country='optional')
    def test_register_sapsf_metadata_present_override_relevant_value(self):
        """
        Configure the provider such that it can talk to a mocked-out version of the SAP SuccessFactors
        API, and ensure that the data it gets that way gets passed to the registration form.

        Check that value mappings overrides work in cases where we override a value other than
        what we're looking for, and when an empty override is provided (expected behavior is that
        existing value maps will be left alone).
        """
        value_map = {'country': {'Australia': 'NZ'}}
        expected_country = 'NZ'
        provider_settings = {
            'sapsf_oauth_root_url': 'http://successfactors.com/oauth/',
            'sapsf_private_key': 'fake_private_key_here',
            'odata_api_root_url': 'http://api.successfactors.com/odata/v2/',
            'odata_company_id': 'NCC1701D',
            'odata_client_id': 'TatVotSEiCMteSNWtSOnLanCtBGwNhGB',
        }
        if value_map:
            provider_settings['sapsf_value_mappings'] = value_map

        self._configure_testshib_provider(
            identity_provider_type='sap_success_factors',
            metadata_source=TESTSHIB_METADATA_URL,
            other_settings=json.dumps(provider_settings)
        )
        self._test_register(country=expected_country)

    @patch.dict('django.conf.settings.REGISTRATION_EXTRA_FIELDS', country='optional')
    def test_register_sapsf_metadata_present_override_other_value(self):
        """
        Configure the provider such that it can talk to a mocked-out version of the SAP SuccessFactors
        API, and ensure that the data it gets that way gets passed to the registration form.

        Check that value mappings overrides work in cases where we override a value other than
        what we're looking for, and when an empty override is provided (expected behavior is that
        existing value maps will be left alone).
        """
        value_map = {'country': {'United States': 'blahfake'}}
        expected_country = 'AU'
        provider_settings = {
            'sapsf_oauth_root_url': 'http://successfactors.com/oauth/',
            'sapsf_private_key': 'fake_private_key_here',
            'odata_api_root_url': 'http://api.successfactors.com/odata/v2/',
            'odata_company_id': 'NCC1701D',
            'odata_client_id': 'TatVotSEiCMteSNWtSOnLanCtBGwNhGB',
        }
        if value_map:
            provider_settings['sapsf_value_mappings'] = value_map

        self._configure_testshib_provider(
            identity_provider_type='sap_success_factors',
            metadata_source=TESTSHIB_METADATA_URL,
            other_settings=json.dumps(provider_settings)
        )
        self._test_register(country=expected_country)

    @patch.dict('django.conf.settings.REGISTRATION_EXTRA_FIELDS', country='optional')
    def test_register_sapsf_metadata_present_empty_value_override(self):
        """
        Configure the provider such that it can talk to a mocked-out version of the SAP SuccessFactors
        API, and ensure that the data it gets that way gets passed to the registration form.

        Check that value mappings overrides work in cases where we override a value other than
        what we're looking for, and when an empty override is provided (expected behavior is that
        existing value maps will be left alone).
        """

        value_map = {'country': {}}
        expected_country = 'AU'
        provider_settings = {
            'sapsf_oauth_root_url': 'http://successfactors.com/oauth/',
            'sapsf_private_key': 'fake_private_key_here',
            'odata_api_root_url': 'http://api.successfactors.com/odata/v2/',
            'odata_company_id': 'NCC1701D',
            'odata_client_id': 'TatVotSEiCMteSNWtSOnLanCtBGwNhGB',
        }
        if value_map:
            provider_settings['sapsf_value_mappings'] = value_map

        self._configure_testshib_provider(
            identity_provider_type='sap_success_factors',
            metadata_source=TESTSHIB_METADATA_URL,
            other_settings=json.dumps(provider_settings)
        )
        self._test_register(country=expected_country)

    def test_register_http_failure(self):
        """
        Ensure that if there's an HTTP failure while fetching metadata, we continue, using the
        metadata from the SAML assertion.
        """
        self._configure_testshib_provider(
            identity_provider_type='sap_success_factors',
            metadata_source=TESTSHIB_METADATA_URL,
            other_settings=json.dumps({
                'sapsf_oauth_root_url': 'http://successfactors.com/oauth-fake/',
                'sapsf_private_key': 'fake_private_key_here',
                'odata_api_root_url': 'http://api.successfactors.com/odata/v2/',
                'odata_company_id': 'NCC1701D',
                'odata_client_id': 'TatVotSEiCMteSNWtSOnLanCtBGwNhGB',
            })
        )
        # Because we're getting details from the assertion, fall back to the initial set of details.
        self.USER_EMAIL = "myself@testshib.org"
        self.USER_NAME = "Me Myself And I"
        self.USER_USERNAME = "myself"
        self._test_register()

    def test_register_http_failure_in_odata(self):
        """
        Ensure that if there's an HTTP failure while fetching user details from
        SAP SuccessFactors OData API.
        """
        # Because we're getting details from the assertion, fall back to the initial set of details.
        self.USER_EMAIL = "myself@testshib.org"
        self.USER_NAME = "Me Myself And I"
        self.USER_USERNAME = "myself"

        odata_company_id = 'NCC1701D'
        odata_api_root_url = 'http://api.successfactors.com/odata/v2/'
        mocked_odata_api_url = self._mock_odata_api_for_error(odata_api_root_url, self.USER_USERNAME)
        self._configure_testshib_provider(
            identity_provider_type='sap_success_factors',
            metadata_source=TESTSHIB_METADATA_URL,
            other_settings=json.dumps({
                'sapsf_oauth_root_url': 'http://successfactors.com/oauth/',
                'sapsf_private_key': 'fake_private_key_here',
                'odata_api_root_url': odata_api_root_url,
                'odata_company_id': odata_company_id,
                'odata_client_id': 'TatVotSEiCMteSNWtSOnLanCtBGwNhGB',
            })
        )
        with LogCapture(level=logging.WARNING) as log_capture:
            self._test_register()
            logging_messages = str([log_msg.getMessage() for log_msg in log_capture.records]).replace('\\', '')
            self.assertIn(odata_company_id, logging_messages)
            self.assertIn(mocked_odata_api_url, logging_messages)
            self.assertIn(self.USER_USERNAME, logging_messages)
            self.assertIn("SAPSuccessFactors", logging_messages)
            self.assertIn("Error message", logging_messages)
            self.assertIn("System message", logging_messages)
            self.assertIn("Headers", logging_messages)

    @skip('Test not necessary for this subclass')
    def test_get_saml_idp_class_with_fake_identifier(self):
        pass

    @skip('Test not necessary for this subclass')
    def test_login(self):
        pass

    @skip('Test not necessary for this subclass')
    def test_register(self):
        pass
