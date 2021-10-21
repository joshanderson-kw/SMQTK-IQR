"""
IQR Search sub-application module
"""
import base64
from io import BytesIO
import json
import os
import os.path as osp
import random
import shutil
from typing import Any, Dict, Hashable, Type, TypeVar, Optional
import zipfile
import logging

import flask
import PIL.Image

from smqtk_dataprovider import DataSet, DataElement
from smqtk_dataprovider.utils.file import safe_create_dir
from smqtk_dataprovider.impls.data_element.file import DataFileElement
from smqtk_core.configuration import (
    Configurable,
    from_config_dict,
    make_default_config,
    to_config_dict
)
from smqtk_iqr.utils.web import ServiceProxy
from smqtk_iqr.web.search_app import IqrSearchDispatcher
from smqtk_iqr.iqr import IqrSession
from smqtk_iqr.utils.mimetype import get_mimetypes
from smqtk_iqr.utils.preview_cache import PreviewCache
from smqtk_iqr.web.search_app.modules.file_upload.FileUploadMod import FileUploadMod
from smqtk_iqr.web.search_app.modules.static_host import StaticDirectoryHost


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG = logging.getLogger(__name__)
T = TypeVar("T", bound="IqrSearch")
MT = get_mimetypes()


class IqrSearch (flask.Flask, Configurable):
    """
    IQR Search Tab blueprint

    Components:
        * Data-set, from which base media data is provided
        * Descriptor generator, which provides descriptor generation services
          for user uploaded data.
        * NearestNeighborsIndex, from which descriptors are queried from user
          input data. This index should contain descriptors that were
          generated by the same descriptor generator configuration above (same
          dimensionality, etc.).
        * RelevancyIndex, which is populated by an initial query, and then
          iterated over within the same user session. A new instance and model
          is generated every time a new session is created (or new data is
          uploaded by the user).

    Assumes:
        * DescriptorElement related to a DataElement have the same UUIDs.

    """

    # TODO: User access white/black-list? See ``search_app/__init__.py``:L135

    @classmethod
    def get_default_config(cls) -> Dict[str, Any]:
        d = super(IqrSearch, cls).get_default_config()

        # Remove parent_app slot for later explicit specification.
        del d['parent_app']

        d['iqr_service_url'] = None

        # fill in plugin configs
        d['data_set'] = make_default_config(DataSet.get_impls())

        return d

    # noinspection PyMethodOverriding
    @classmethod
    def from_config(  # type: ignore
        cls: Type[T], config: Dict[str, Any],
        parent_app: IqrSearchDispatcher
    ) -> T:
        """
        Instantiate a new instance of this class given the configuration
        JSON-compliant dictionary encapsulating initialization arguments.

        :param config: JSON compliant dictionary encapsulating
            a configuration.

        :param parent_app: Parent containing flask app instance

        :return: Constructed instance from the provided config.

        """
        merged = cls.get_default_config()
        merged.update(config)

        # construct nested objects via configurations
        merged['data_set'] = \
            from_config_dict(merged['data_set'], DataSet.get_impls())

        return cls(parent_app, **merged)

    def __init__(
        self, parent_app: IqrSearchDispatcher, iqr_service_url: str,
        data_set: DataSet, working_directory: str
    ):
        """
        Initialize a generic IQR Search module with a single descriptor and
        indexer.

        :param parent_app: Parent containing flask app instance

        :param iqr_service_url: Base URL to the IQR service to use for this
            application interface. Any trailing slashes will be striped.

        :param data_set: DataSet of the content described by indexed descriptors
            in the linked IQR service.

        :param working_directory: Directory in which to place working files.
            These may be considered temporary and may be removed between
            executions of this app.

        :raises ValueError: Invalid Descriptor or indexer type

        """
        super(IqrSearch, self).__init__(
            import_name=__name__,
            static_folder=os.path.join(SCRIPT_DIR, "static"),
            template_folder=os.path.join(SCRIPT_DIR, "templates"),
        )

        self._parent_app = parent_app
        self._data_set = data_set
        self._iqr_service = ServiceProxy(iqr_service_url.rstrip('/'))

        # base directory that's transformed by the ``work_dir`` property into
        # an absolute path.
        self._working_dir = working_directory
        # Directory to put things to allow them to be statically available to
        # public users.
        self._static_data_prefix = "static/data"
        self._static_data_dir = osp.join(self.work_dir, 'static')

        # Custom static host sub-module
        self.mod_static_dir = StaticDirectoryHost('%s_static' % self.name,
                                                  self._static_data_dir,
                                                  self._static_data_prefix)
        self.register_blueprint(self.mod_static_dir)

        # Uploader Sub-Module
        self.upload_work_dir = os.path.join(self.work_dir, "uploads")
        self.mod_upload = FileUploadMod('%s_uploader' % self.name, parent_app,
                                        self.upload_work_dir,
                                        url_prefix='/uploader')
        self.register_blueprint(self.mod_upload)
        self.register_blueprint(parent_app.module_login)

        # Mapping of session IDs to their work directory
        self._iqr_work_dirs: Dict[str, str] = {}
        # Mapping of session ID to a dictionary of the custom example data for
        # a session (uuid -> DataElement)
        self._iqr_example_data: Dict[
            str,
            Dict[Hashable, DataElement]
        ] = {}

        # Preview Image Caching
        self._preview_cache = PreviewCache(osp.join(self._static_data_dir,
                                                    "previews"))

        # Cache mapping of written static files for data elements
        self._static_cache: Dict[Hashable, str] = {}
        self._static_cache_element: Dict[Hashable, DataElement] = {}

        #
        # Routing
        #

        @self.route("/")
        @self._parent_app.module_login.login_required
        def index() -> str:
            # Stripping left '/' from blueprint modules in order to make sure
            # the paths are relative to our base.
            assert self.mod_upload.url_prefix is not None, (
                "Currently assuming the upload module has a non-None URL "
                "prefix."
            )
            r = {
                "module_name": self.name,
                "uploader_url": self.mod_upload.url_prefix.lstrip('/'),
                "uploader_post_url":
                    self.mod_upload.upload_post_url().lstrip('/'),
            }
            LOG.debug("Uploader URL: %s", r['uploader_url'])
            # noinspection PyUnresolvedReferences
            return flask.render_template("iqr_search_index.html", **r)

        @self.route('/iqr_session_info', methods=["GET"])
        @self._parent_app.module_login.login_required
        def iqr_session_info() -> flask.Response:
            """
            Get information about the current IRQ session
            """
            sid = self.get_current_iqr_session()
            get_r = self._iqr_service.get('session', sid=sid)
            get_r.raise_for_status()
            return flask.jsonify(get_r.json())

        @self.route('/get_iqr_state')
        @self._parent_app.module_login.login_required
        def iqr_session_state() -> flask.Response:
            """
            Get IQR session state information composed of positive and negative
            descriptor vectors.

            We append to the state received from the service in order to produce
            a state byte package that is compatible with the
            ``IqrSession.set_state_bytes`` method. This way state bytes received
            from this function can be directly consumed by the IQR service or
            other IqrSession instances.

            """
            sid = self.get_current_iqr_session()

            # Get the state base64 from the underlying service.
            r_get = self._iqr_service.get('state', sid=sid)
            r_get.raise_for_status()
            state_b64 = r_get.json()['state_b64']
            state_bytes = base64.b64decode(state_b64)

            # Load state dictionary from base-64 ZIP payload from service
            # - GET content is base64, so decode first and then read as a
            #   ZipFile buffer.
            # - `r_get.content` is `byte` type so it can be passed directly to
            #   base64 decode.
            state_dict = json.load(
                zipfile.ZipFile(
                    BytesIO(state_bytes),
                    'r',
                    IqrSession.STATE_ZIP_COMPRESSION
                ).open(IqrSession.STATE_ZIP_FILENAME)
            )
            r_get.close()

            # Wrap service state with our UI state: uploaded data elements.
            # Data elements are stored as a dictionary mapping UUID to MIMETYPE
            # and data byte string.
            working_data = {}
            sid_data_elems: Dict[Hashable, DataElement] = self._iqr_example_data.get(sid, {})
            for uid in sid_data_elems:
                # Decoding base64 as ASCII knowing that
                # `base64.urlsafe_b64decode` is used later, whose doc-string
                # states that it may expect an ASCII string when not bytes.
                working_data[uid] = {
                    'content_type': sid_data_elems[uid].content_type(),
                    'bytes_base64':
                        base64.b64encode(sid_data_elems[uid].get_bytes())
                              .decode('ascii'),
                }

            state_dict["working_data"] = working_data
            state_json = json.dumps(state_dict)

            z_wrapper_buffer = BytesIO()
            z_wrapper = zipfile.ZipFile(z_wrapper_buffer, 'w',
                                        IqrSession.STATE_ZIP_COMPRESSION)
            z_wrapper.writestr(IqrSession.STATE_ZIP_FILENAME, state_json)
            z_wrapper.close()

            z_wrapper_buffer.seek(0)
            return flask.send_file(
                z_wrapper_buffer,
                mimetype='application/octet-stream',
                as_attachment=True,
                attachment_filename="%s.IqrState" % sid
            )

        @self.route('/set_iqr_state', methods=['PUT'])
        @self._parent_app.module_login.login_required
        def set_iqr_session_state() -> flask.Response:
            """
            Set the current state based on the given state file.
            """
            sid = self.get_current_iqr_session()
            fid = flask.request.form.get('fid', None)

            return_obj: Dict[str, Any] = {
                'success': False,
            }

            #
            # Load in state zip package, prepare zip package for service
            #

            if fid is None:
                return_obj['message'] = 'No file ID provided.'

            LOG.debug("[%s::%s] Getting temporary filepath from "
                      "uploader module", sid, fid)
            assert fid is not None
            upload_filepath = self.mod_upload.get_path_for_id(fid)
            self.mod_upload.clear_completed(fid)

            # Load ZIP package back in, then remove the uploaded file.
            try:
                z = zipfile.ZipFile(
                    upload_filepath,
                    compression=IqrSession.STATE_ZIP_COMPRESSION
                )
                with z.open(IqrSession.STATE_ZIP_FILENAME) as f:
                    state_dict = json.load(f)
                z.close()
            finally:
                os.remove(upload_filepath)

            #
            # Consume working data UUID/bytes
            #
            # Reset this server's resources for an SID
            self.reset_session_local(sid)
            # - Dictionary of data UUID (SHA1) to {'content_type': <str>,
            #   'bytes_base64': <str>} dictionary.
            working_data: Dict[str, Dict] = state_dict['working_data']
            del state_dict['working_data']
            # - Write out base64-decoded files to session-specific work
            #   directory.
            # - Update self._iqr_example_data with DataFileElement instances
            #   referencing the just-written files.
            for uuid_sha1 in working_data:
                data_mimetype = working_data[uuid_sha1]['content_type']
                data_b64 = str(working_data[uuid_sha1]['bytes_base64'])
                # Output file to working directory on disk.
                data_filepath = os.path.join(
                    self._iqr_work_dirs[sid],
                    '%s%s' % (uuid_sha1, MT.guess_extension(data_mimetype))
                )
                with open(data_filepath, 'wb') as f:
                    f.write(base64.urlsafe_b64decode(data_b64))
                # Create element reference and store it for the current session.
                data_elem = DataFileElement(data_filepath, readonly=True)
                self._iqr_example_data[sid][uuid_sha1] = data_elem

            #
            # Re-package service state as a ZIP payload.
            #
            service_zip_buffer = BytesIO()
            service_zip = zipfile.ZipFile(service_zip_buffer, 'w',
                                          IqrSession.STATE_ZIP_COMPRESSION)
            service_zip.writestr(IqrSession.STATE_ZIP_FILENAME,
                                 json.dumps(state_dict))
            service_zip.close()
            service_zip_base64 = \
                base64.b64encode(service_zip_buffer.getvalue())

            # Update service state
            self._iqr_service.put('state',
                                  sid=sid,
                                  state_base64=service_zip_base64)

            return flask.jsonify(return_obj)

        @self.route("/check_current_iqr_session")
        @self._parent_app.module_login.login_required
        def check_current_iqr_session() -> flask.Response:
            """
            Check that the current IQR session exists and is initialized.

            Return JSON:
                success
                    Always True if the message returns.

            """
            # Getting the current IQR session ensures that one has been
            # constructed for the current session.
            _ = self.get_current_iqr_session()
            return flask.jsonify({
                "success": True
            })

        @self.route("/get_data_preview_image", methods=["GET"])
        @self._parent_app.module_login.login_required
        def get_ingest_item_image_rep() -> flask.Response:
            """
            Return the base64 preview image data link for the data file
            associated with the give UID (plus some other metadata).
            """
            uid = flask.request.args['uid']

            info: Dict[str, Any] = {
                "success": True,
                "message": None,
                "shape": None,  # (width, height)
                "static_file_link": None,
                "static_preview_link": None,
            }

            # Try to find a DataElement by the given UUID in our indexed data
            # or in the session's example data.
            de: Optional[DataElement]
            if self._data_set.has_uuid(uid):
                de = self._data_set.get_data(uid)
            else:
                sid = self.get_current_iqr_session()
                de = self._iqr_example_data[sid].get(uid, None)

            if not de:
                info["success"] = False
                info["message"] = "UUID '%s' not part of the base or working " \
                                  "data set!" % uid
            else:
                # Preview_path should be a path within our statically hosted
                # area.
                preview_path = self._preview_cache.get_preview_image(de)
                img = PIL.Image.open(preview_path)
                info["shape"] = img.size

                if de.uuid() not in self._static_cache:
                    self._static_cache[de.uuid()] = \
                        de.write_temp(self._static_data_dir)
                    self._static_cache_element[de.uuid()] = de

                # Need to format links by transforming the generated paths to
                # something usable by webpage:
                # - make relative to the static directory, and then pre-pending
                #   the known static url to the
                info["static_preview_link"] = \
                    self._static_data_prefix + '/' + \
                    os.path.relpath(preview_path, self._static_data_dir)
                info['static_file_link'] = \
                    self._static_data_prefix + '/' + \
                    os.path.relpath(self._static_cache[de.uuid()],
                                    self._static_data_dir)

            return flask.jsonify(info)

        @self.route('/iqr_ingest_file', methods=['POST'])
        @self._parent_app.module_login.login_required
        def iqr_ingest_file() -> str:
            """
            Ingest the file with the given UID, getting the path from the
            uploader.

            :return: string of data/descriptor element's UUID

            """
            # TODO: Add status dict with a "GET" method branch for getting that
            #       status information.

            fid = flask.request.form['fid']

            sid = self.get_current_iqr_session()

            LOG.debug("[%s::%s] Getting temporary filepath from "
                      "uploader module", sid, fid)
            upload_filepath = self.mod_upload.get_path_for_id(fid)
            self.mod_upload.clear_completed(fid)

            LOG.debug("[%s::%s] Moving uploaded file", sid, fid)
            sess_upload = osp.join(self._iqr_work_dirs[sid],
                                   osp.basename(upload_filepath))
            os.rename(upload_filepath, sess_upload)

            # Record uploaded data as user example data for this session.
            upload_data = DataFileElement(sess_upload)
            uuid = upload_data.uuid()
            self._iqr_example_data[sid][uuid] = upload_data

            # Extend session ingest -- modifying
            LOG.debug("[%s::%s] Adding new data to session "
                      "external positives", sid, fid)
            data_b64 = base64.b64encode(upload_data.get_bytes())
            data_ct = upload_data.content_type()
            r = self._iqr_service.post('add_external_pos', sid=sid,
                                       base64=data_b64, content_type=data_ct)
            r.raise_for_status()

            return str(uuid)

        @self.route("/iqr_initialize", methods=["POST"])
        @self._parent_app.module_login.login_required
        def iqr_initialize() -> flask.Response:
            """
            Initialize IQR session working index based on current positive
            examples and adjudications.
            """
            sid = self.get_current_iqr_session()

            # (Re)Initialize working index
            post_r = self._iqr_service.post('initialize', sid=sid)
            post_r.raise_for_status()

            return flask.jsonify(post_r.json())

        @self.route("/get_example_adjudication", methods=["GET"])
        @self._parent_app.module_login.login_required
        def get_example_adjudication() -> flask.Response:
            """
            Get positive/negative status for a data/descriptor in our example
            set.

            :return: {
                    is_pos: <bool>,
                    is_neg: <bool>
                }

            """
            # TODO: Collapse example and index adjudication endpoints.
            elem_uuid = flask.request.args['uid']
            sid = self.get_current_iqr_session()
            get_r = self._iqr_service.get('adjudicate', sid=sid, uid=elem_uuid)
            get_r.raise_for_status()
            get_r_json = get_r.json()
            return flask.jsonify({
                "is_pos": get_r_json['is_pos'],
                "is_neg": get_r_json['is_neg'],
            })

        @self.route("/get_index_adjudication", methods=["GET"])
        @self._parent_app.module_login.login_required
        def get_index_adjudication() -> flask.Response:
            """
            Get the adjudication status of a particular data/descriptor element
            by UUID.

            This should only ever return a dict where one of the two, or
            neither, are labeled True.

            :return: {
                    is_pos: <bool>,
                    is_neg: <bool>
                }
            """
            # TODO: Collapse example and index adjudication endpoints.
            elem_uuid = flask.request.args['uid']
            sid = self.get_current_iqr_session()
            get_r = self._iqr_service.get('adjudicate', sid=sid, uid=elem_uuid)
            get_r.raise_for_status()
            get_r_json = get_r.json()
            return flask.jsonify({
                "is_pos": get_r_json['is_pos'],
                "is_neg": get_r_json['is_neg'],
            })

        @self.route("/adjudicate", methods=["POST"])
        @self._parent_app.module_login.login_required
        def adjudicate() -> flask.Response:
            """
            Update adjudication for this session. This should specify UUIDs of
            data/descriptor elements in our working index.

            :return: {
                    success: <bool>,
                    message: <str>
                }
            """
            pos_to_add = json.loads(flask.request.form.get('add_pos', '[]'))
            pos_to_remove = json.loads(flask.request.form.get('remove_pos',
                                                              '[]'))
            neg_to_add = json.loads(flask.request.form.get('add_neg', '[]'))
            neg_to_remove = json.loads(flask.request.form.get('remove_neg',
                                                              '[]'))

            msg = "Adjudicated Positive{+%s, -%s}, " \
                  "Negative{+%s, -%s} " \
                  % (pos_to_add, pos_to_remove,
                     neg_to_add, neg_to_remove)
            LOG.debug(msg)

            sid = self.get_current_iqr_session()

            to_neutral = list(set(pos_to_remove) | set(neg_to_remove))

            post_r = self._iqr_service.post('adjudicate',
                                            sid=sid,
                                            pos=json.dumps(pos_to_add),
                                            neg=json.dumps(neg_to_add),
                                            neutral=json.dumps(to_neutral))
            post_r.raise_for_status()

            return flask.jsonify({
                "success": True,
                "message": msg
            })

        @self.route("/iqr_refine", methods=["POST"])
        @self._parent_app.module_login.login_required
        def iqr_refine() -> flask.Response:
            """
            Classify current IQR session indexer, updating ranking for
            display.

            Fails gracefully if there are no positive[/negative] adjudications.

            """
            sid = self.get_current_iqr_session()
            post_r = self._iqr_service.post('refine', sid=sid)
            post_r.raise_for_status()
            return flask.jsonify({
                "success": True,
                "message": "Completed refinement",
            })

        @self.route("/iqr_ordered_results", methods=['GET'])
        @self._parent_app.module_login.login_required
        def get_ordered_results() -> flask.Response:
            """
            Get ordered (UID, probability) pairs in between the given indices,
            [i, j). If j Is beyond the end of available results, only available
            results are returned.

            This may be empty if no refinement has yet occurred.

            Return format:
            {
                results: [ (uid, probability), ... ]
            }
            """
            i = flask.request.args.get('i', None)
            j = flask.request.args.get('j', None)

            params = {
                'sid': self.get_current_iqr_session(),
            }
            if i is not None:
                params['i'] = i
            if j is not None:
                params['j'] = j

            get_r = self._iqr_service.get('get_results', **params)
            get_r.raise_for_status()
            return flask.jsonify(get_r.json())

        @self.route("/reset_iqr_session", methods=["POST"])
        @self._parent_app.module_login.login_required
        def reset_iqr_session() -> flask.Response:
            """
            Reset the current IQR session
            """
            sid = self.get_current_iqr_session()
            # Reset service
            put_r = self._iqr_service.put('session', sid=sid)
            put_r.raise_for_status()
            # Reset local server resources
            self.reset_session_local(sid)
            return flask.jsonify({"success": True})

        @self.route("/get_random_uids")
        @self._parent_app.module_login.login_required
        def get_random_uids() -> flask.Response:
            """
            Return to the client a list of data/descriptor IDs available in the
            configured data set (NOT descriptor/NNI set).

            Thus, we assume that the nearest neighbor index that is searchable
            is from at least this set of data.

            :return: {
                    uids: list[str]
                }
            """
            all_ids = list(self._data_set.uuids())
            random.shuffle(all_ids)
            return flask.jsonify({
                "uids": all_ids
            })

        @self.route('/is_ready')
        def is_ready() -> flask.Response:
            """ Simple 'I'm alive' endpoint """
            return flask.jsonify({
                "alive": True,
            })

    def __del__(self) -> None:
        for wdir in self._iqr_work_dirs.values():
            if os.path.isdir(wdir):
                shutil.rmtree(wdir)

    def get_config(self) -> Dict[str, Any]:
        return {
            'iqr_service_url': self._iqr_service.url,
            'working_directory': self._working_dir,
            'data_set': to_config_dict(self._data_set),
        }

    @property
    def work_dir(self) -> str:
        """
        :return: Common work directory for this instance.
        """
        return osp.expanduser(osp.abspath(self._working_dir))

    def get_current_iqr_session(self) -> str:
        """
        Get the current IQR Session UUID.
        """
        sid = str(flask.session.sid)  # type: ignore

        # Ensure there is an initialized session on the configured service.
        created_session = False
        get_r = self._iqr_service.get('session_ids')
        get_r.raise_for_status()
        if sid not in get_r.json()['session_uuids']:
            post_r = self._iqr_service.post('session', sid=sid)
            post_r.raise_for_status()
            created_session = True

        if created_session or (sid not in self._iqr_work_dirs):
            # Dictionaries not initialized yet for this UUID.
            self._iqr_work_dirs[sid] = osp.join(self.work_dir, sid)
            self._iqr_example_data[sid] = {}

            safe_create_dir(self._iqr_work_dirs[sid])

        return sid

    def reset_session_local(self, sid: str) -> None:
        """
        Reset elements of this server for a given session ID.

        A given ``sid`` must have been created first. This happens in the
        ``get_current_iqr_session`` method.

        This does not affect the linked IQR service.

        :param sid: Session ID to reset for.

        :raises KeyError: ``sid`` not recognized. Probably not initialized
            first.

        """
        # Also clear work sub-directory and example data state
        if os.path.isdir(self._iqr_work_dirs[sid]):
            shutil.rmtree(self._iqr_work_dirs[sid])
        safe_create_dir(self._iqr_work_dirs[sid])

        self._iqr_example_data[sid].clear()
