"""This module implements the AppFutures.

    We have two basic types of futures:
    1. DataFutures which represent data objects
    2. AppFutures which represent the futures on App/Leaf tasks.

"""

from concurrent.futures import Future
import logging
import threading

from parsl.app.errors import RemoteException

logger = logging.getLogger(__name__)

# Possible future states (for internal use by the futures package).
PENDING = 'PENDING'
RUNNING = 'RUNNING'
# The future was cancelled by the user...
CANCELLED = 'CANCELLED'
# ...and _Waiter.add_cancelled() was called by a worker.
CANCELLED_AND_NOTIFIED = 'CANCELLED_AND_NOTIFIED'
FINISHED = 'FINISHED'

_STATE_TO_DESCRIPTION_MAP = {
    PENDING: "pending",
    RUNNING: "running",
    CANCELLED: "cancelled",
    CANCELLED_AND_NOTIFIED: "cancelled",
    FINISHED: "finished"
}


class AppFuture(Future):
    """An AppFuture points at a Future returned from an Executor.

    We are simply wrapping a AppFuture, and adding the specific case where, if the future
    is resolved i.e file exists, then the DataFuture is assumed to be resolved.

    """

    def set_result(self, result):
        logger.debug("{} setting result".format(self))
        self.commit(1)
        if self.parent is not None:
            raise ValueException("trying to operate on super when we have a parent callback")
        super().set_result(result)

    def set_exception(self, e):
        logger.debug("{} setting exception".format(self))
        self.commit(1)
        if self.parent is not None:
            raise ValueException("trying to operate on super when we have a parent callback")
        super().set_exception(e)

    def parent_callback(self, executor_fu):
        """Callback from executor future to update the parent.

        Args:
            - executor_fu (Future): Future returned by the executor along with callback

        Returns:
            - None

        Updates the super() with the result() or exception()
        """
        # print("[RETRY:TODO] parent_Callback for {0}".format(executor_fu))

        logger.debug("{} parent callback to update super()".format(self))

        self.commit(2)

        # this is an unexpected situation, I think
        if executor_fu != self.parent:
            raise ValueException("parent_callback received an executor_fu {} that differs from self.parent {}".format(executor_fu, self.parent))

        if self.parent is not None:
            raise ValueException("trying to operate on super when we have a parent callback")


        if executor_fu.done() is True:
            try:
                super().set_result(executor_fu.result())
            except Exception as e:
                super().set_exception(e)

    def __init__(self, parent, tid=None, stdout=None, stderr=None):
        """Initialize the AppFuture.

        Args:
             - parent (Future) : The parent future if one exists
               A default value of None should be passed in if app is not launched

        KWargs:
             - tid (Int) : Task id should be any unique identifier. Now Int.
             - stdout (str) : Stdout file of the app.
                   Default: None
             - stderr (str) : Stderr file of the app.
                   Default: None
        """
        self._tid = tid
        super().__init__()
        self.prev_parent = None
        self.parent = parent
        self._parent_update_lock = threading.Lock()
        self._parent_update_event = threading.Event()
        self._outputs = []
        self._stdout = stdout
        self._stderr = stderr
        self._commit = 0
        self._commit_lock = threading.Lock()

    @property
    def stdout(self):
        return self._stdout

    @property
    def stderr(self):
        return self._stderr

    @property
    def tid(self):
        return self._tid

    def update_parent(self, fut):
        """Add a callback to the parent to update the state.

        This handles the case where the user has called result on the AppFuture
        before the parent exists.
        """
        if self.parent is not None:
            raise ValueException("Can't set parent multiple times")
        self.commit(2)
        logger.debug("Future {} updating parent from {} to {}".format(self,self.parent, fut))
        # with self._parent_update_lock:
        self.parent = fut
        fut.add_done_callback(self.parent_callback)
        self._parent_update_event.set()

    def result(self, timeout=None):
        """Result.

        Waits for the result of the AppFuture
        KWargs:
              timeout (int): Timeout in seconds
        """
        try:
            if self.parent:
                self.commit(2)

                # but what if we have a result already in *this* future?
                if super().done():
                    raise ValueException("super() is done, but we're trying to wait for a result from self.parent")


                logger.debug("{} waiting for result from self.parent {}".format(self, self.parent))
                res = self.parent.result(timeout=timeout)
            else:
                self.commit(1)
                logger.debug("{} waiting for result from super() as no parent".format(self))
                res = super().result(timeout=timeout)

            if isinstance(res, RemoteException):
                res.reraise()
            return res

        except Exception as e:
            logger.debug("{} exception handling, this app future has parent of {}".format(self, self.parent))
            if self.parent and self.parent.retries_left > 0:
                self.commit(2)
                logger.debug("{} exception handling: {} retries remaining, so waiting again for result".format(self, self.parent.retries_left))
                self._parent_update_event.wait()
                self._parent_update_event.clear()
                return self.result(timeout=timeout)
            else:
                # no parent, or no retries left
                if isinstance(e, RemoteException):
                    e.reraise()
                else:
                    raise

    def cancel(self):
        if self.parent:
            self.commit(2)
            return self.parent.cancel
        else:
            return False

    def cancelled(self):
        if self.parent:
            self.commit(2)
            return self.parent.cancelled()
        else:
            return False

    def running(self):
        if self.parent:
            self.commit(2)
            return self.parent.running()
        else:
            return False

    def done(self):
        """Check if the future is done.

        If a parent is set, we return the status of the parent.
        else, there is no parent assigned, meaning the status is False.

        Returns:
              - True : If the future has successfully resolved.
              - False : Pending resolution
        """
        if self.parent:
            self.commit(2)
            return self.parent.done()
        else:
            return False

    def exception(self, timeout=None):
        if self.parent:
            self.commit(2)
            return self.parent.exception(timeout=timeout)
        else:
            return False

    def add_done_callback(self, fn):
        if self.parent:
            self.commit(2)
            return self.parent.add_done_callback(fn)
        else:
            raise ValueException("Attempted to add_done_callback, but discarding instead")

    @property
    def outputs(self):
        return self._outputs

    def commit(self, n):
        with self._commit_lock:
            if(self._commit == 0):
                logger.debug("{} fresh-committing to path {}".format(self, n))
                self._commit = n
            elif self._commit == n:
                logger.debug("{} re-committing to path {}".format(self, n))
            else:
                logger.debug("COMMIT FAILURE {} cannot commit to path {} as already committed to {}".format(self, n, self._commit))

    def __repr__(self):
        if self.parent:
            with self.parent._condition:
                if self.parent._state == FINISHED:
                    if self.parent._exception:
                        return '<%s at %#x state=%s raised %s>' % (
                            self.__class__.__name__,
                            id(self),
                            _STATE_TO_DESCRIPTION_MAP[self.parent._state],
                            self.parent._exception.__class__.__name__)
                    else:
                        return '<%s at %#x state=%s returned %s>' % (
                            self.__class__.__name__,
                            id(self),
                            _STATE_TO_DESCRIPTION_MAP[self.parent._state],
                            self.parent._result.__class__.__name__)
                return '<%s at %#x state=%s>' % (
                    self.__class__.__name__,
                    id(self),
                    _STATE_TO_DESCRIPTION_MAP[self.parent._state])
        else:
            return '<%s at %#x state=%s>' % (
                self.__class__.__name__,
                id(self),
                _STATE_TO_DESCRIPTION_MAP[self._state])
