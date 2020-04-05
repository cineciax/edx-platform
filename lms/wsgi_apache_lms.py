"""
Apache WSGI file for LMS

This module contains the WSGI application used for Apache deployment.
It exposes a module-level variable named ``application``.
"""


import os

# This application object is used by the development server
# as well as any WSGI server configured to use this file.
from django.core.wsgi import get_wsgi_application

import lms.startup as startup
from openedx.core.lib.logsettings import log_python_warnings
# Patch the xml libs before anything else.
from safe_lxml import defuse_xml_libs

log_python_warnings()

defuse_xml_libs()


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lms.envs.aws")
os.environ.setdefault("SERVICE_VARIANT", "lms")

startup.run()

application = get_wsgi_application()  # pylint: disable=invalid-name
