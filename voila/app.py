#############################################################################
# Copyright (c) 2018, Voila Contributors                                    #
#                                                                           #
# Distributed under the terms of the BSD 3-Clause License.                  #
#                                                                           #
# The full license is in the file LICENSE, distributed with this software.  #
#############################################################################

from zmq.eventloop import ioloop
import os
import shutil
import signal
import tempfile
import logging
import gettext

import jinja2

import tornado.ioloop
import tornado.web

from traitlets.config.application import Application
from traitlets import Unicode, Integer, Bool, Dict, List, default

from jupyter_server.services.kernels.kernelmanager import MappingKernelManager
from jupyter_server.services.kernels.handlers import KernelHandler, ZMQChannelsHandler
from jupyter_server.base.handlers import path_regex
from jupyter_server.services.contents.largefilemanager import LargeFileManager
from jupyter_server.utils import url_path_join
from jupyter_server.services.config import ConfigManager
from jupyter_server.base.handlers import FileFindHandler
from jupyter_core.paths import jupyter_config_path, jupyter_path
from ipython_genutils.py3compat import getcwd

from .paths import ROOT, STATIC_ROOT, collect_template_paths
from .handler import VoilaHandler
from .treehandler import VoilaTreeHandler
from ._version import __version__
from .static_file_handler import MultiStaticFileHandler

ioloop.install()
_kernel_id_regex = r"(?P<kernel_id>\w+-\w+-\w+-\w+-\w+)"


class Voila(Application):
    name = 'voila'
    version = __version__
    examples = 'voila example.ipynb --port 8888'
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
    strip_sources = Bool(True, help='Strip sources from rendered html').tag(config=True)
    port = Integer(
        8866,
        config=True,
        help='Port of the voila server. Default 8866.'
    )
    autoreload = Bool(
        False,
        config=True,
        help='Will autoreload to server and the page when a template, js file or Python code changes'
    )
    root_dir = Unicode(config=True, help="The directory to use for notebooks.")
    static_root = Unicode(
        STATIC_ROOT,
        config=True,
        help='Directory holding static assets (HTML, JS and CSS files).'
    )
    aliases = {
        'port': 'Voila.port',
        'static': 'Voila.static_root',
        'strip_sources': 'Voila.strip_sources',
        'autoreload': 'Voila.autoreload',
        'template': 'Voila.template',
        'base_url': 'Voila.base_url',
        'server_url': 'Voila.server_url',
    }
    connection_dir_root = Unicode(
        config=True,
        help=(
            'Location of temporry connection files. Defaults '
            'to system `tempfile.gettempdir()` value.'
        )
    )
    connection_dir = Unicode()

    base_url = Unicode(
        '/',
        config=True,
        help=(
            'Path for voila API calls. If server_url is unset, this will be \
            used for both the base route of the server and the client. \
            If server_url is set, the server will server the routes prefixed \
            by server_url, while the client will prefix by base_url (this is \
            useful in reverse proxies).')
    )

    server_url = Unicode(
        None,
        config=True,
        allow_none=True,
        help=(
            'Path to prefix to voila API handlers. Leave unset to default to base_url')
    )

    template = Unicode(
        'default',
        config=True,
        allow_none=True,
        help=(
            'template name to be used by voila.'
        )
    )

    notebook_path = Unicode(
        None,
        config=True,
        allow_none=True,
        help=(
            'path to notebook to serve with voila')
    )

    nbconvert_template_paths = List(
        [],
        config=True,
        help=(
            'path to nbconvert templates'
        )
    )

    template_paths = List(
        [],
        allow_none=True,
        config=True,
        help=(
            'path to nbconvert templates'
        )
    )

    static_paths = List(
        [STATIC_ROOT],
        config=True,
        help=(
            'paths to static assets'
        )
    )

    config_file_paths = List(Unicode(), config=True, help='Paths to search for voila.(py|json)')

    tornado_settings = Dict(
        {},
        config=True,
        help=(
            'Extra settings to apply to tornado application, e.g. headers, ssl, etc'
        )
    )

    extra_extensions = List(
        [],
        config=True,
        help=(
            'This setting can be used to pass in extra extensions to requirejs, e.g. \
             lab-only extensions, custom CDNs, non-standard paths, etc'
        )
    )

    @default('config_file_paths')
    def _config_file_paths_default(self):
        return [os.getcwd()] + jupyter_config_path()

    @default('connection_dir_root')
    def _default_connection_dir(self):
        connection_dir = tempfile.gettempdir()
        self.log.info('Using %s to store connection files' % connection_dir)
        return connection_dir

    @default('log_level')
    def _default_log_level(self):
        return logging.INFO

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

    def initialize(self, argv=None):
        self.log.debug("Searching path %s for config files", self.config_file_paths)
        # to make config_file_paths settable via cmd line, we first need to parse it
        super(Voila, self).initialize(argv)
        self.notebook_path = self.notebook_path if self.notebook_path else self.extra_args[0] if len(self.extra_args) == 1 else None
        # then we load the config
        self.load_config_file('voila', path=self.config_file_paths)
        # but that cli config has preference, so we overwrite with that
        self.update_config(self.cli_config)
        self.setup_template_dirs()
        signal.signal(signal.SIGTERM, self._handle_signal_stop)

    def setup_template_dirs(self):
        if self.template:
            collect_template_paths(
                self.nbconvert_template_paths,
                self.static_paths,
                self.template_paths,
                self.template)
        self.log.debug('using template: %s', self.template)
        self.log.debug('nbconvert template paths: %s', self.nbconvert_template_paths)
        self.log.debug('template paths: %s', self.template_paths)
        self.log.debug('static paths: %s', self.static_paths)
        if self.notebook_path and not os.path.exists(self.notebook_path):
            raise ValueError('Notebook not found: %s' % self.notebook_path)

    def _handle_signal_stop(self, sig, frame):
        self.log.info('Handle signal %s.' % sig)
        self.ioloop.add_callback_from_signal(self.ioloop.stop)

    def start(self):
        self.connection_dir = tempfile.mkdtemp(
            prefix='voila_',
            dir=self.connection_dir_root
        )
        self.log.info('Storing connection files in %s.' % self.connection_dir)
        self.log.info('Serving static files from %s.' % self.static_root)

        self.kernel_manager = MappingKernelManager(
            parent=self,
            connection_dir=self.connection_dir,
            allowed_message_types=[
                'comm_msg',
                'comm_info_request',
                'kernel_info_request',
                'shutdown_request'
            ]
        )

        jenv_opt = {"autoescape": True}  # we might want extra options via cmd line like notebook server
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(self.template_paths), extensions=['jinja2.ext.i18n'], **jenv_opt)
        nbui = gettext.translation('nbui', localedir=os.path.join(ROOT, 'i18n'), fallback=True)
        env.install_gettext_translations(nbui, newstyle=False)
        self.contents_manager = LargeFileManager(parent=self)

        # we create a config manager that load both the serverconfig and nbconfig (classical notebook)
        read_config_path = [os.path.join(p, 'serverconfig') for p in jupyter_config_path()]
        read_config_path += [os.path.join(p, 'nbconfig') for p in jupyter_config_path()]
        self.config_manager = ConfigManager(parent=self, read_config_path=read_config_path)

        # default server_url to base_url
        self.server_url = self.server_url or self.base_url

        self.app = tornado.web.Application(
            base_url=self.base_url,
            server_url=self.server_url or self.base_url,
            kernel_manager=self.kernel_manager,
            allow_remote_access=True,
            autoreload=self.autoreload,
            voila_jinja2_env=env,
            jinja2_env=env,
            static_path='/',
            server_root_dir='/',
            contents_manager=self.contents_manager,
            config_manager=self.config_manager
        )

        self.app.settings.update(self.tornado_settings)

        handlers = []

        handlers.extend([
            (url_path_join(self.server_url, r'/api/kernels/%s' % _kernel_id_regex), KernelHandler),
            (url_path_join(self.server_url, r'/api/kernels/%s/channels' % _kernel_id_regex), ZMQChannelsHandler),
            (
                url_path_join(self.server_url, r'/voila/static/(.*)'),
                MultiStaticFileHandler,
                {
                    'paths': self.static_paths,
                    'default_filename': 'index.html'
                }
            )
        ])

        # this handler serves the nbextensions similar to the classical notebook
        handlers.append(
            (
                url_path_join(self.server_url, r'/voila/nbextensions/(.*)'),
                FileFindHandler,
                {
                    'path': self.nbextensions_path,
                    'no_cache_paths': ['/'],  # don't cache anything in nbextensions
                },
            )
        )

        if self.notebook_path:
            handlers.append((
                url_path_join(self.server_url, r'/'),
                VoilaHandler,
                {
                    'notebook_path': os.path.relpath(self.notebook_path, self.root_dir),
                    'strip_sources': self.strip_sources,
                    'nbconvert_template_paths': self.nbconvert_template_paths,
                    'template_name': self.template,
                    'config': self.config
                }
            ))
        else:
            self.log.debug('serving directory: %r', self.root_dir)
            handlers.extend([
                (self.server_url, VoilaTreeHandler),
                (url_path_join(self.server_url, r'/voila/tree' + path_regex), VoilaTreeHandler),
                (url_path_join(self.server_url, r'/voila/render' + path_regex), VoilaHandler,
                    {
                        'strip_sources': self.strip_sources,
                        'nbconvert_template_paths': self.nbconvert_template_paths,
                        'config': self.config,
                        'extra_extensions': self.extra_extensions
                    }),
            ])

        self.app.add_handlers('.*$', handlers)
        self.listen()

    def listen(self):
        self.app.listen(self.port)
        self.log.info('Voila listening on port %s.' % self.port)

        self.ioloop = tornado.ioloop.IOLoop.current()
        try:
            self.ioloop.start()
        except KeyboardInterrupt:
            self.log.info('Stopping...')
        finally:
            shutil.rmtree(self.connection_dir)
            self.kernel_manager.shutdown_all()


main = Voila.launch_instance
