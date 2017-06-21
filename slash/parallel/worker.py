import threading
import logbook
import os
import pickle
import time
from six.moves import xmlrpc_client
from .server import FINISHED_ALL_TESTS, PROTOCOL_ERROR, WAITING_FOR_CLIENTS, SHOULD_TERMINATE
from ..exceptions import INTERRUPTION_EXCEPTIONS
from ..hooks import register
from ..ctx import context
from ..runner import run_tests
from ..conf import config

_logger = logbook.Logger(__name__)

class Worker(object):
    def __init__(self, client_id, session_id, collected_tests):
        super(Worker, self).__init__()
        self.client_id = client_id
        self.session_id = session_id
        self.collected_tests = collected_tests
        self.server_addr = 'http://{0}:{1}'.format(config.root.parallel.server_addr, config.root.parallel.server_port)

    def keep_alive(self, stop_event):
        proxy = xmlrpc_client.ServerProxy(self.server_addr)
        while not stop_event.is_set():
            proxy.keep_alive(self.client_id)
            stop_event.wait(1)

    def warning_added(self, warning):
        try:
            warning = pickle.dumps(warning)
            self.client.report_warning(self.client_id, warning)
        except (pickle.PicklingError, TypeError):
            _logger.error("Failed to pickle warning. Message: {}, File: {}, Line: {}".format(warning.message, warning.filename, warning.lineno))

    def start(self, app):
        if not os.getpid() == os.getpgid(0):
            os.setsid()
        register(self.warning_added)
        collection = [(test.__slash__.file_path, test.__slash__.function_name, test.__slash__.variation.id) for test in self.collected_tests]
        self.client = xmlrpc_client.ServerProxy(self.server_addr, allow_none=True)
        self.client.connect(self.client_id)
        if not self.client.validate_collection(self.client_id, collection):
            _logger.error("Collections of worker id {0} and master don't match, worker terminates".format(self.client_id))
            self.client.disconnect(self.client_id)
            return

        stop_event = threading.Event()
        tr = threading.Thread(target=self.keep_alive, args=(stop_event,))
        tr.setDaemon(True)
        tr.start()
        retries_num = 0
        should_stop = False
        with app.session.get_started_context():
            try:
                while not should_stop:
                    test_index = self.client.get_test(self.client_id)
                    if test_index == WAITING_FOR_CLIENTS:
                        _logger.debug("Worker_id {} recieved waiting_for_clients, sleeping".format(self.client_id))
                        time.sleep(0.05)
                    elif test_index == PROTOCOL_ERROR:
                        _logger.error("Worker_id {} recieved protocol error message, terminating".format(self.client_id))
                        break
                    elif test_index == FINISHED_ALL_TESTS:
                        _logger.debug("Got FINISHED_ALL_TESTS, Client {} disconnecting".format(self.client_id))
                        break
                    elif test_index == SHOULD_TERMINATE:
                        _logger.debug("Got SHOULD_TERMINATE, Client {} disconnecting".format(self.client_id))
                        break
                    else:
                        test = self.collected_tests[test_index]
                        context.session.current_parallel_test_index = test_index
                        run_tests([test])
                        result = context.session.results[test]
                        _logger.debug("Client {} finished test, sending results".format(self.client_id))
                        ret = self.client.finished_test(self.client_id, result.serialize())
                        if ret == PROTOCOL_ERROR:
                            _logger.error("Worker_id {} recieved protocol error message, terminating".format(self.client_id))
                            should_stop = True
            except INTERRUPTION_EXCEPTIONS:
                _logger.error("Worker interrupted while executing test")
                raise
            else:
                context.session.mark_complete()
                self.client.disconnect(self.client_id)
            finally:
                context.session.scope_manager.flush_remaining_scopes()
                stop_event.set()
                tr.join()