#############################################################################
# Copyright (c) 2018, Voila Contributors                                    #
#                                                                           #
# Distributed under the terms of the BSD 3-Clause License.                  #
#                                                                           #
# The full license is in the file LICENSE, distributed with this software.  #
#############################################################################

from zmq.eventloop import ioloop

import gettext
import io
import logging
import threading
import tempfile
import os
import shutil
import signal
import socket
import webbrowser

try:
    from urllib.parse import urljoin
    from urllib.request import pathname2url
except ImportError:
    from urllib import pathname2url
    from urlparse import urljoin

import jinja2

import tornado.ioloop
import tornado.web

from traitlets.config.application import Application
from traitlets import Unicode, Integer, Bool, Dict, List, default

from jupyter_server.services.kernels.kernelmanager import MappingKernelManager
from jupyter_server.services.kernels.handlers import KernelHandler, ZMQChannelsHandler
from jupyter_server.services.contents.largefilemanager import LargeFileManager
from jupyter_server.base.handlers import path_regex
from jupyter_server.utils import url_path_join
from jupyter_server.services.config import ConfigManager
from jupyter_server.base.handlers import FileFindHandler
from jupyter_server.extension.application import ExtensionApp
from jupyter_core.paths import jupyter_config_path, jupyter_path

from ipython_genutils.py3compat import getcwd

from .paths import ROOT, STATIC_ROOT, collect_template_paths, notebook_path_regex
from .handler import VoilaHandler
from .treehandler import VoilaTreeHandler
from ._version import __version__

ioloop.install()
_kernel_id_regex = r"(?P<kernel_id>\w+-\w+-\w+-\w+-\w+)"


def _(x):
    return x


class Voila(ExtensionApp):
    name = 'voila'
    version = __version__
    examples = 'voila example.ipynb --port 8888'
    
    extension_name = 'voila'

    flags = {
        'no-browser': ({'Voila': {'open_browser': False}}, _('Don\'t open the notebook in a browser after startup.'))
    }

    description = Unicode(
        """voila [OPTIONS] NOTEBOOK_FILENAME

        This launches a stand-alone server for read-only notebooks.
        """
    )
    option_description = Unicode(
        """
        notebook_path:
            File name of the Jupyter notebook to display.
        """
    )
    notebook_filename = Unicode()

    autoreload = Bool(
        False,
        config=True,
        help=_(
            'Will autoreload to server and the page when a template, js file or Python code changes'
        )
    )
    root_dir = Unicode(config=True, help=_('The directory to use for notebooks.'))
    static_root = Unicode(
        STATIC_ROOT,
        config=True,
        help=_(
            'Directory holding static assets (HTML, JS and CSS files).'
        )
    )
    aliases = {
        'port': 'Voila.port',
        'static': 'Voila.static_root',
        'strip_sources': 'VoilaConfiguration.strip_sources',
        'autoreload': 'Voila.autoreload',
        'template': 'VoilaConfiguration.template',
        'theme': 'VoilaConfiguration.theme',
        'base_url': 'Voila.base_url',
        'server_url': 'Voila.server_url',
        'enable_nbextensions': 'VoilaConfiguration.enable_nbextensions'
    }

    template = Unicode(
        'default',
        config=True,
        allow_none=True,
        help=(
            'template name to be used by voila.'
        )
    )
    theme = Unicode('light').tag(config=True)
    strip_sources = Bool(True, help='Strip sources from rendered html').tag(config=True)
    enable_nbextensions = Bool(False, config=True, help=('Set to True for Voila to load notebook extensions'))

    connection_dir_root = Unicode(
        config=True,
        help=_(
            'Location of temporry connection files. Defaults '
            'to system `tempfile.gettempdir()` value.'
        )
    )
    connection_dir = Unicode()

    base_url = Unicode(
        '/',
        config=True,
        help=_(
            'Path for voila API calls. If server_url is unset, this will be \
            used for both the base route of the server and the client. \
            If server_url is set, the server will server the routes prefixed \
            by server_url, while the client will prefix by base_url (this is \
            useful in reverse proxies).'
        )
    )

    server_url = Unicode(
        None,
        config=True,
        allow_none=True,
        help=_(
            'Path to prefix to voila API handlers. Leave unset to default to base_url'
        )
    )

    notebook_path = Unicode(
        None,
        config=True,
        allow_none=True,
        help=_(
            'path to notebook to serve with voila'
        )
    )

    nbconvert_template_paths = List(
        [],
        config=True,
        help=_(
            'path to nbconvert templates'
        )
    )

    template_paths = List(
        [],
        allow_none=True,
        config=True,
        help=_(
            'path to nbconvert templates'
        )
    )

    static_paths = List(
        [STATIC_ROOT],
        config=True,
        help=_(
            'paths to static assets'
        )
    )

    webbrowser_open_new = Integer(2, config=True,
                                  help=_("""Specify Where to open the notebook on startup. This is the
                                  `new` argument passed to the standard library method `webbrowser.open`.
                                  The behaviour is not guaranteed, but depends on browser support. Valid
                                  values are:
                                  - 2 opens a new tab,
                                  - 1 opens a new window,
                                  - 0 opens in an existing window.
                                  See the `webbrowser.open` documentation for details.
                                  """))

    custom_display_url = Unicode(u'', config=True,
                                 help=_("""Override URL shown to users.
                                 Replace actual URL, including protocol, address, port and base URL,
                                 with the given value when displaying URL to the users. Do not change
                                 the actual connection URL. If authentication token is enabled, the
                                 token is added to the custom URL automatically.
                                 This option is intended to be used when the URL to display to the user
                                 cannot be determined reliably by the Jupyter notebook server (proxified
                                 or containerized setups for example)."""))

    config_file_paths = List(
        Unicode(),
        config=True,
        help=_(
            'Paths to search for voila.(py|json)'
        )
    )

    @default('config_file_paths')
    def _config_file_paths_default(self):
        return [os.getcwd()] + jupyter_config_path()

    # similar to NotebookApp, except no extra path
    @property
    def nbextensions_path(self):
        """The path to look for Javascript notebook extensions"""
        path = jupyter_path('nbextensions')
        # FIXME: remove IPython nbextensions path after a migration period
        try:
            from IPython.paths import get_ipython_dir
        except ImportError:
            pass
        else:
            path.append(os.path.join(get_ipython_dir(), 'nbextensions'))
        return path

    @default('root_dir')
    def _default_root_dir(self):
        if self.notebook_path:
            return os.path.dirname(os.path.abspath(self.notebook_path))
        else:
            return getcwd()


    default_url = Unicode("/voila", config=True)

    def initialize_templates(self):
        # common configuration options between the server extension and the application
        collect_template_paths(
            self.nbconvert_template_paths,
            self.static_paths,
            self.template_paths,
            self.template
        )
        jenv_opt = {"autoescape": True}
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(self.template_paths), extensions=['jinja2.ext.i18n'], **jenv_opt)

        nbui = gettext.translation('nbui', localedir=os.path.join(ROOT, 'i18n'), fallback=True)
        env.install_gettext_translations(nbui, newstyle=False)

        template_settings = dict(
            voila_template_paths=self.template_paths,
            voila_jinja2_env=env,
            nbconvert_template_paths=self.nbconvert_template_paths
        )
        self.settings.update(**template_settings)

    def initialize_settings(self):
        voila_configuration = dict(
            template=self.template,
            theme=self.theme,
            strip_sources=self.strip_sources,
            enable_nbextensions=self.enable_nbextensions,
            notebook_path=self.notebook_path,
        )
        self.settings['voila_configuration'] = voila_configuration

    def initialize_handlers(self):
        handlers = [
            ('/voila/render' + path_regex, VoilaHandler),
            ('/voila', VoilaTreeHandler),
            ('/voila/tree' + path_regex, VoilaTreeHandler),
        ]
        self.handlers.extend(handlers)

main = Voila.launch_instance
