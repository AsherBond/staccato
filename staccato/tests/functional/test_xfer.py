import filecmp
import time

import staccato.xfer.interface as xfer_iface
import staccato.xfer.constants as xfer_consts
import staccato.db as db
import staccato.xfer.constants as constants
from staccato.common import config
from staccato.tests import utils


class FakeStateMachine(object):

    def event_occurred(self, *args, **kwvals):
        pass


class TestXfer(utils.TempFileCleanupBaseTest):

    def setUp(self):
        super(TestXfer, self).setUp()
        self.conf = config.get_config_object(args=[])
        self.tmp_db = self.get_tempfile()
        self.db_url = 'sqlite:///%s' % (self.tmp_db)

        conf_d = {'sql_connection': self.db_url,
                  'protocol_policy': ''}

        self.conf_file = self.make_confile(conf_d, utils.FILE_ONLY_PROTOCOL)
        self.conf = config.get_config_object(
            args=[],
            default_config_files=[self.conf_file])

    def test_file_xfer_basic(self):
        dst_file = self.get_tempfile()
        src_file = "/bin/bash"
        src_url = "file://%s" % src_file
        dst_url = "file://%s" % dst_file

        xfer = xfer_iface.xfer_new(self.conf, src_url, dst_url,
                                   {}, {}, 0, None)
        xfer_iface.xfer_start(self.conf, xfer.id)

        db_obj = db.StaccatoDB(self.conf)
        while not xfer_consts.is_state_done_running(xfer.state):
            time.sleep(0.01)
            xfer = db_obj.lookup_xfer_request_by_id(xfer.id)

        self.assertTrue(filecmp.cmp(dst_file, src_file))
        self.assertTrue(xfer.state, constants.States.STATE_COMPLETE)

    def test_file_xfer_cancel(self):
        dst_file = self.get_tempfile()
        src_file = "/dev/zero"
        src_url = "file://%s" % src_file
        dst_url = "file://%s" % dst_file

        xfer = xfer_iface.xfer_new(self.conf, src_url, dst_url,
                                   {}, {}, 0, None)
        xfer_iface.xfer_start(self.conf, xfer.id)
        xfer_iface.xfer_cancel(self.conf, xfer.id)

        db_obj = db.StaccatoDB(self.conf)
        while not xfer_consts.is_state_done_running(xfer.state):
            time.sleep(0.01)
            xfer = db_obj.lookup_xfer_request_by_id(xfer.id)

        self.assertTrue(xfer.state, constants.States.STATE_CANCELED)
