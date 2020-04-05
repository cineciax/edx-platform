"""
WSGI config for LMS.

This module contains the WSGI application used by Django's development server
and any production WSGI deployments.
It exposes a module-level variable named ``application``. Django's
``runserver`` and ``runfcgi`` commands discover this application via the
``WSGI_APPLICATION`` setting.
"""


import os

# Disable PyContract contract checking when running as a webserver
import contracts
# This application object is used by the development server
# as well as any WSGI server configured to use this file.
from django.core.wsgi import get_wsgi_application

import lms.startup as startup
from openedx.core.lib.logsettings import log_python_warnings
# Patch the xml libs
from safe_lxml import defuse_xml_libs
from xmodule.modulestore.django import modulestore

log_python_warnings()

defuse_xml_libs()

contracts.disable_all()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lms.envs.aws")

startup.run()


# Trigger a forced initialization of our modulestores since this can take a
# while to complete and we want this done before HTTP requests are accepted.
modulestore()


application = get_wsgi_application()  # pylint: disable=invalid-name
