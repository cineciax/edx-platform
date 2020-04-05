"""
WSGI config for CMS.

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

import cms.startup as startup
from openedx.core.lib.logsettings import log_python_warnings
# Patch the xml libs before anything else.
from safe_lxml import defuse_xml_libs

log_python_warnings()

defuse_xml_libs()

contracts.disable_all()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cms.envs.aws")

startup.run()

application = get_wsgi_application()
