#!/usr/bin/env python
# -*- coding:utf-8 -*-

from saml2 import (
    BINDING_HTTP_POST,
    BINDING_HTTP_REDIRECT,
    entity,
)
from saml2.client import Saml2Client
from saml2.config import Config as Saml2Config

from django import get_version
from pkg_resources import parse_version
from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, logout, get_user_model
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.template import TemplateDoesNotExist
from django.http import HttpResponseRedirect
from django.utils.http import is_safe_url
from logging import getLogger
from book.models import Organization
from integration.serializers import ReferralCreatorSerializer
from django.shortcuts import redirect
from django.urls import reverse

logger = getLogger('django-saml2-auth')

# Default User
User = get_user_model()

# Get imports based on installed versions
try:
    import urllib2 as _urllib
except:
    import urllib.request as _urllib
    import urllib.error
    import urllib.parse

# Get import based on installed version
if parse_version(get_version()) >= parse_version('1.7'):
    from django.utils.module_loading import import_string
else:
    from django.utils.module_loading import import_by_path as import_string

# Helper function to obtain the default next url in the saml schema
def _default_next_url():
    if 'DEFAULT_NEXT_URL' in settings.SAML2_AUTH:
        return settings.SAML2_AUTH['DEFAULT_NEXT_URL']
    else:
        return '/rfr/dashboard'

# Helper function to obtain the current domain (assertion url) in the saml schema
def get_current_domain(r):
    if 'ASSERTION_URL' in settings.SAML2_AUTH:
        return settings.SAML2_AUTH['ASSERTION_URL']
    return '{scheme}://{host}'.format(
        scheme='https' if r.is_secure() else 'http', 
        host=r.get_host(),
    )

# Helper function to call reverse() on a list of objects
def get_reverse(objects):
    if parse_version(get_version()) >= parse_version('2.0'):
        from django.urls import reverse
    else:
        from django.core.urlresolvers import reverse
    if objects.__class__.__name__ not in ['list', 'tuple']:
        objects = [objects]

    for obj in objects:
        try:
            return reverse(obj)
        except:
            pass
    raise Exception('URL reverse issue: %s.  Known issue from fangli/django-saml2-auth' % str(objects))

# Helper function to obtain the metadata in the saml schema
def _get_metadata():
    if 'METADATA_LOCAL_FILE_PATH' in settings.SAML2_AUTH:
        return {
            'local': [settings.SAML2_AUTH['METADATA_LOCAL_FILE_PATH']]
        }
    else:
        return {
            'remote': [
                {
                    "url": settings.SAML2_AUTH['METADATA_AUTO_CONF_URL'],
                },
            ]
        }

# Helper function to obtain the saml client given the domain
def _get_saml_client(domain):
    acs_url = domain + get_reverse([acs, 'acs', 'django_saml2_auth:acs'])
    metadata = _get_metadata()

    saml_settings = {
        'metadata': metadata,
        'service': {
            'sp': {
                'endpoints': {
                    'assertion_consumer_service': [
                        (acs_url, BINDING_HTTP_REDIRECT),
                        (acs_url, BINDING_HTTP_POST)
                    ],
                },
                'allow_unsolicited': True,
                'authn_requests_signed': False,
                'logout_requests_signed': True,
                'want_assertions_signed': True,
                'want_response_signed': False,
            },
        },
    }

    if 'ENTITY_ID' in settings.SAML2_AUTH:
        saml_settings['entityid'] = settings.SAML2_AUTH['ENTITY_ID']

    if 'NAME_ID_FORMAT' in settings.SAML2_AUTH:
        saml_settings['service']['sp']['name_id_format'] = settings.SAML2_AUTH['NAME_ID_FORMAT']

    spConfig = Saml2Config()
    spConfig.load(saml_settings)
    spConfig.allow_unknown_attributes = True
    saml_client = Saml2Client(config=spConfig)
    return saml_client

# Helper function to render the custom welcome template if it exists
@login_required
def welcome(r):
    try:
        return render(r, 'django_saml2_auth/welcome.html', {'user': r.user})
    except TemplateDoesNotExist:
        return HttpResponseRedirect(_default_next_url())

# Helper function to render the denied template
def denied(r):
    return render(r, 'django_saml2_auth/denied.html')

# Helper function to create a new user and associated lead_creator
def _create_new_user(username, email, firstname, lastname):
    logger.debug('_create_new_user')
    # Create a new user object with the parameters passed
    # Limit user name to allowable length in DB
    username = str(username)[:150]
    user = User.objects.create(email=email,
                               username=username, 
                               first_name=firstname, 
                               last_name=lastname,)

    # Obtain the Customer group instance
    group = Group.objects.get(name='Customers')

    # Set user properties according to SAML2_AUTH configuration
    user.groups.add(group)
    user.is_active = settings.SAML2_AUTH.get('NEW_USER_PROFILE', {}).get('ACTIVE_STATUS', True) # Default to true if not found
    user.is_staff = settings.SAML2_AUTH.get('NEW_USER_PROFILE', {}).get('STAFF_STATUS', False) # Default to false if not found
    user.is_superuser = settings.SAML2_AUTH.get('NEW_USER_PROFILE', {}).get('SUPERUSER_STATUS', False) # Default to false if not found
    user.set_password(settings.TCH_DEFAULT_PASS)

    # Save changes to the new user instance
    user.save()
    logger.debug('new user success')

    # Add user to tch lead creator instance
    try:
        org = Organization.objects.get(name="Texas Children's Hospital - Pathology")
    except Organization.DoesNotExist:
        raise Exception('Organization does not exist')

    # Instantiate a new lead creator
    leadCreator = ReferralCreatorSerializer(data={'organization': org.id, 'django_user': user.id})
    if leadCreator.is_valid():
        leadCreator.save()
        logger.debug('New lead creator success')
    else:
        logger.debug('New lead creator failure')
    
    return user

# View function to handle logic after SSO user login
@csrf_exempt
def acs(r):
    logger.debug('acs')
    saml_client = _get_saml_client(get_current_domain(r))
    resp = r.POST.get('SAMLResponse', None)
    next_url = r.session.get('login_next_url', settings.SAML2_AUTH.get('DEFAULT_NEXT_URL', '/rfr/dashboard'))

    if not resp:
        return HttpResponseRedirect(get_reverse([denied, 'denied', 'django_saml2_auth:denied']))

    authn_response = saml_client.parse_authn_request_response(resp, entity.BINDING_HTTP_POST)
    if authn_response is None:
        return HttpResponseRedirect(get_reverse([denied, 'denied', 'django_saml2_auth:denied']))
    
    user_identity = authn_response.get_identity()
    if user_identity is None:
        return HttpResponseRedirect(get_reverse([denied, 'denied', 'django_saml2_auth:denied']))

    # For Azure Active Directory Mapping
    user_email = user_identity[settings.SAML2_AUTH.get('ATTRIBUTES_MAP', {}).get('email', 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name')][0]
    user_name = user_identity[settings.SAML2_AUTH.get('ATTRIBUTES_MAP', {}).get('username', 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name')][0]
    user_first_name = user_identity[settings.SAML2_AUTH.get('ATTRIBUTES_MAP', {}).get('first_name', 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname')][0]
    user_last_name = user_identity[settings.SAML2_AUTH.get('ATTRIBUTES_MAP', {}).get('last_name', 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname')][0]

    target_user = None
    is_new_user = False

    # Try to query the user by the username
    try:
        target_user = User.objects.get(username=user_name)
    # If the User DNE, create a new user with their provided credentials
    except User.DoesNotExist:
        # Create a new user
        target_user = _create_new_user(user_name, user_email, user_first_name, user_last_name)
        new_user = True
			  
    # If the user is active, we want to login
    if target_user.is_active:
        logger.debug('trying to authenticate')
        # Authenticate the user
        target_user.backend = 'django.contrib.auth.backends.ModelBackend'
        login(r, target_user)
        return redirect(reverse('lead_creator_dashboard'))
    else:
        return HttpResponseRedirect(get_reverse([denied, 'denied', 'django_saml2_auth:denied']))

# View function to redirect to client default SSO login
def signin(r):
    try:
        import urlparse as _urlparse
        from urllib import unquote
    except:
        import urllib.parse as _urlparse
        from urllib.parse import unquote
    next_url = r.GET.get('next', _default_next_url())

    try:
        if 'next=' in unquote(next_url):
            next_url = _urlparse.parse_qs(_urlparse.urlparse(unquote(next_url)).query)['next'][0]
    except:
        next_url = r.GET.get('next', _default_next_url())
    
    # Only permit signin requests where the next_url is a safe URL
    if parse_version(get_version()) >= parse_version('2.0'):
        url_ok = is_safe_url(next_url, None)
    else:
        url_ok = is_safe_url(next_url)

    if not url_ok:
        return HttpResponseRedirect(get_reverse([denied, 'denied', 'django_saml2_auth:denied']))

    r.session['login_next_url'] = next_url

    saml_client = _get_saml_client(get_current_domain(r))
    _, info = saml_client.prepare_for_authenticate()

    redirect_url = None

    for key, value in info['headers']:
        if key == 'Location':
            redirect_url = value
            break
    
    return HttpResponseRedirect(redirect_url)

# Helper function to render a custom signout template
def signout(r):
    logout(r)
    return render(r, 'django_saml2_auth/signout.html')
