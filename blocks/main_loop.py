"""The event-based main loop of Blocks."""
import signal
import logging
import time
import traceback
from collections import defaultdict

from blocks import config
from blocks.log import TrainingLog
from blocks.utils import reraise_as, unpack, change_recursion_limit
from blocks.utils.containers import OrderedSet
from blocks.algorithms import DifferentiableCostMinimizer
from blocks.extensions import CallbackName

logger = logging.getLogger(__name__)

error_message = """

Blocks will attempt to run `on_error` extensions, potentially saving data, \
before exiting and reraising the error. Note that the usual `after_training` \
extensions will *not* be run. The original error will be re-raised and also \
stored in the training log. Press CTRL + C to halt Blocks immediately."""

error_in_error_handling_message = """

Blocks will now exit. The remaining `on_error` extensions will not be run."""


epoch_interrupt_message = """

Blocks will complete this epoch iteration of training and run extensions \
before exiting. If you do not want to complete this epoch, press CTRL + C \
again to stop training after the current batch."""

batch_interrupt_message = """

Blocks will complete the current batch and run extensions before exiting. If \
you do not want to complete this batch, press CTRL + C again. WARNING: Note \
that this will end training immediately, and extensions that e.g. save your \
training progress won't be run."""

no_model_message = """

A possible reason: one of your extensions requires the main loop to have \
a model. Check documentation of your extensions."""


class Timer(object):
    def __init__(self):
        self.total = defaultdict(int)
        self.current = []
        self.order = OrderedSet()

    def enter(self, name):
        self.current.append(name)
        # We record the order in which sections were first called
        self.order.add(tuple(self.current))

    def exit(self, t):
        self.total[tuple(self.current)] += t
        self.current.pop()

    def report(self):
        """Print a report of timing information to standard output.

        .. todo::

           This method could accept different I/O objects (e.g. standard
           error or write to a file).

        """
        total = sum(v for k, v in self.total.items() if len(k) == 1)

        def print_report(keys, level=0):
            subtotal = 0
            for key in keys:
                if len(key) > level + 1:
                    continue
                subtotal += self.total[key]
                section = ' '.join(key[-1].split('_'))
                section = section[0].upper() + section[1:]
                print('{:30}{:15.2f}{:15.2%}'.format(
                    level * '  ' + section, self.total[key],
                    self.total[key] / total
                ))
                children = [k for k in keys
                            if k[level] == key[level] and
                            len(k) > level + 1]
                child_total = print_report(children, level + 1)
                if children:
                    print('{:30}{:15.2f}{:15.2%}'.format(
                        (level + 1) * '  ' + 'Other',
                        self.total[key] - child_total,
                        (self.total[key] - child_total) / total
                    ))
            return subtotal

        print('{:30}{:>15}{:>15}'.format('Section', 'Time', '% of total'))
        print('-' * 60)
        print_report(self.order)


class TimeIt(object):
    """A context manager to time the execution time of code within it.

    Parameters
    ----------
    name : str
        The name of this section. Expected to adhere to variable naming
        styles.
    timer : :class:`Timer`
        The timer of the main loop. This is the object this context manager
        will report the execution time to. The accumulation and processing
        of timing information is handled by this object.

    """
    def __init__(self, name, timer):
        self.name = name
        self.timer = timer

    def __enter__(self):
        self.timer.enter(self.name)
        self.start = time.clock()

    def __exit__(self, *args):
        self.timer.exit(time.clock() - self.start)


class MainLoop(object):
    """The standard main loop of Blocks.

    In the `MainLoop` a model is trained by a training algorithm using data
    extracted from a data stream. This process is scrupulously documented
    in a log object.

    The `MainLoop` itself does very little: only fetching the data from the
    data stream and feeding it to the algorithm. It expects the extensions
    to do most of the job. A respective callback of every extension is
    called at every stage of training. The extensions should communicate
    between themselves and with the main loop object by means of making
    records in the log. For instance in order to stop the training
    procedure an extension can make a record
    `training_finish_requested=True` in the log. The main loop checks for
    such a record after every batch and every epoch and terminates when
    finds it.

    The `MainLoop` also handles interruption signal SIGINT for you (e.g.
    the one program receives when you press Ctrl + C). It notes this event
    in the log and at the next iteration or epoch end the main loop will
    be gracefully finished, with calling all necessary extension callbacks
    and waiting until they finish.

    Parameters
    ----------
    algorithm : object
        The training algorithm.
    data_stream : instance of :class:`.DataStream`.
        The data stream.
    model : :class:`.AbstractModel` instance, optional
        The model object. It is entirely transparent for the main loop
        but may be used by extensions.
    log : instance of :class:`.TrainingLog`, optional
        The log. When not given, a :class:`.TrainingLog` is created.
    extensions : list of :class:`.TrainingExtension` instances
        The training extensions. Will be called in the same order as given
        here.

    """
    def __init__(self, algorithm, data_stream,
                 model=None, log=None, extensions=None):
        if not log:
            log = TrainingLog()
        if not extensions:
            extensions = []

        self.data_stream = data_stream
        self.algorithm = algorithm
        self.log = log
        self.extensions = extensions

        self.timer = Timer()

        self._model = model

        self.status['training_started'] = False
        self.status['epoch_started'] = False
        self.status['epoch_interrupt_received'] = False
        self.status['batch_interrupt_received'] = False

    @property
    def model(self):
        if not self._model:
            raise AttributeError("no model in this main loop" +
                                 no_model_message)
        return self._model

    @property
    def iteration_state(self):
        """Quick access to the (data stream, epoch iterator) pair."""
        return (self.data_stream, self.epoch_iterator)

    @iteration_state.setter
    def iteration_state(self, value):
        (self.data_stream, self.epoch_iterator) = value

    @property
    def status(self):
        """A shortcut for `self.log.status`."""
        return self.log.status

    def run(self):
        """Starts the main loop.

        The main loop ends when a training extension makes
        a `training_finish_requested` record in the log.

        """
        # This should do nothing if the user has already configured
        # logging, and will it least enable error messages otherwise.
        logging.basicConfig()

        if self._model and isinstance(self.algorithm,
                                      DifferentiableCostMinimizer):
            # Sanity check: model and algorithm should be configured
            # similarly.
            if not self._model.get_objective() == self.algorithm.cost:
                logger.warning("different costs for model and algorithm")
            if not (set(self._model.get_params().values()) ==
                    set(self.algorithm.params)):
                logger.warning("different params for model and algorithm")

        with change_recursion_limit(config.recursion_limit):
            self.original_sigint_handler = signal.signal(
                signal.SIGINT, self._handle_epoch_interrupt)
            self.original_sigterm_handler = signal.signal(
                signal.SIGTERM, self._handle_batch_interrupt)
            try:
                logger.info("Entered the main loop")
                if not self.status['training_started']:
                    for extension in self.extensions:
                        extension.main_loop = self
                    self._run_extensions('before_training')
                    with TimeIt('initialization', self.timer):
                        self.algorithm.initialize()
                    self.status['training_started'] = True
                # We can not write "else:" here because extensions
                # called "before_training" could have changed the status
                # of the main loop.
                if self.log.status['iterations_done'] > 0:
                    self._run_extensions('on_resumption')
                    self.status['epoch_interrupt_received'] = False
                    self.status['batch_interrupt_received'] = False
                with TimeIt('training', self.timer):
                    while self._run_epoch():
                        pass
            except TrainingFinish:
                self.log.current_row['training_finished'] = True
            except Exception as e:
                self._restore_signal_handlers()
                self.log.current_row['got_exception'] = traceback.format_exc(e)
                logger.error("Error occured during training." + error_message)
                try:
                    self._run_extensions('on_error')
                except Exception as inner_e:
                    logger.error(traceback.format_exc(inner_e))
                    logger.error("Error occured when running extensions." +
                                 error_in_error_handling_message)
                reraise_as(e)
            finally:
                if self.log.current_row.get('training_finished', False):
                    self._run_extensions('after_training')
                if config.profile:
                    self.timer.report()
                self._restore_signal_handlers()

    def find_extension(self, name):
        """Find an extension with a given name.

        Parameters
        ----------
        name : str
            The name of the extension looked for.

        Notes
        -----
        Will crash if there no or several extension found.

        """
        return unpack([extension for extension in self.extensions
                       if extension.name == name], singleton=True)

    def _run_epoch(self):
        if not self.status.get('epoch_started', False):
            try:
                self.log.status['received_first_batch'] = False
                self.epoch_iterator = (self.data_stream.
                                       get_epoch_iterator(as_dict=True))
            except StopIteration:
                return False
            self.status['epoch_started'] = True
            self._run_extensions('before_epoch')
        with TimeIt('epoch', self.timer):
            while self._run_iteration():
                pass
        self.status['epoch_started'] = False
        self.status['epochs_done'] += 1
        self.status['_epoch_ends'].append(self.status['iterations_done'])
        self._run_extensions('after_epoch')
        self._check_finish_training('epoch')
        return True

    def _run_iteration(self):
        try:
            with TimeIt('read_data', self.timer):
                batch = next(self.epoch_iterator)
        except StopIteration:
            if not self.log.status['received_first_batch']:
                reraise_as(ValueError("epoch iterator yielded zero batches"))
            return False
        self.log.status['received_first_batch'] = True
        self._run_extensions('before_batch', batch)
        with TimeIt('batch', self.timer):
            self.algorithm.process_batch(batch)
        self.status['iterations_done'] += 1
        self._run_extensions('after_batch', batch)
        self._check_finish_training('batch')
        return True

    def _run_extensions(self, method_name, *args):
        with TimeIt(method_name, self.timer):
            for extension in self.extensions:
                with TimeIt(type(extension).__name__, self.timer):
                    extension.dispatch(CallbackName(method_name), *args)

    def _check_finish_training(self, level):
        """Checks whether the current training should be terminated.

        Parameters
        ----------
        level : {'epoch', 'batch'}
            The level at which this check was performed. In some cases, we
            only want to quit after completing the remained of the epoch.

        """
        # In case when keyboard interrupt is handled right at the end of
        # the iteration the corresponding log record can be found only in
        # the previous row.
        if (self.log.current_row.get('training_finish_requested', False) or
                self.status.get('batch_interrupt_received', False)):
            raise TrainingFinish
        if (level == 'epoch' and
                self.status.get('epoch_interrupt_received', False)):
            raise TrainingFinish

    def _handle_epoch_interrupt(self, signal_number, frame):
        # Try to complete the current epoch if user presses CTRL + C
        logger.warning('Received epoch interrupt signal.' +
                       epoch_interrupt_message)
        signal.signal(signal.SIGINT, self._handle_batch_interrupt)
        self.log.current_row['epoch_interrupt_received'] = True
        # Add a record to the status. Unlike the log record it will be
        # easy to access at later iterations.
        self.status['epoch_interrupt_received'] = True

    def _handle_batch_interrupt(self, signal_number, frame):
        # After 2nd CTRL + C or SIGTERM signal (from cluster) finish batch
        self._restore_signal_handlers()
        logger.warning('Received batch interrupt signal.' +
                       batch_interrupt_message)
        self.log.current_row['batch_interrupt_received'] = True
        # Add a record to the status. Unlike the log record it will be
        # easy to access at later iterations.
        self.status['batch_interrupt_received'] = True

    def _restore_signal_handlers(self):
        signal.signal(signal.SIGINT, self.original_sigint_handler)
        signal.signal(signal.SIGTERM, self.original_sigterm_handler)


class TrainingFinish(Exception):
    """An exception raised when a finish request is found in the log."""
    pass
