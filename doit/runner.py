"""Task runner"""

import sys
from multiprocessing import Process, Queue as MQueue
from threading import Thread
import six
from six.moves import queue, xrange
import pickle

from .exceptions import InvalidTask, CatchedException
from .exceptions import TaskFailed, SetupError, DependencyError, UnmetDependency
from .task import DelayedLoaded

# execution result.
SUCCESS = 0
FAILURE = 1
ERROR = 2

class Runner(object):
    """Task runner

    run_all()
      run_tasks():
        for each task:
            select_task()
            execute_task()
            process_task_result()
      finish()

    """
    def __init__(self, dep_manager, reporter, continue_=False,
                 always_execute=False, verbosity=0):
        """
        @param dep_manager: DependencyBase
        @param reporter: reporter object to be used
        @param continue_: (bool) execute all tasks even after a task failure
        @param always_execute: (bool) execute even if up-to-date or ignored
        @param verbosity: (int) 0,1,2 see Task.execute
        """
        self.dep_manager = dep_manager
        self.reporter = reporter
        self.continue_ = continue_
        self.always_execute = always_execute
        self.verbosity = verbosity

        self.teardown_list = [] # list of tasks to be teardown
        self.final_result = SUCCESS # until something fails
        self._stop_running = False

    def __getstate__(self):
        pickle_dict = self.__dict__.copy()
        pickle_dict['dep_manager'] = None
        return pickle_dict 
        
    def _handle_task_error(self, node, catched_excp):
        """handle all task failures/errors

        called whenever there is an error before executing a task or
        its execution is not successful.
        """
        assert isinstance(catched_excp, CatchedException)
        node.run_status = "failure"
        self.dep_manager.remove_success(node.task)
        self.reporter.add_failure(node.task, catched_excp)
        # only return FAILURE if no errors happened.
        if isinstance(catched_excp, TaskFailed) and self.final_result != ERROR:
            self.final_result = FAILURE
        else:
            self.final_result = ERROR
        if not self.continue_:
            self._stop_running = True


    def _get_task_args(self, task, tasks_dict):
        """get values from other tasks"""
        def get_value(task_id, key_name):
            """get single value or dict from task's saved values"""
            if key_name is None:
                return self.dep_manager.get_values(task_id)
            return self.dep_manager.get_value(task_id, key_name)

        # selected just need to get values from other tasks
        for arg, value in six.iteritems(task.getargs):
            task_id, key_name = value

            if tasks_dict[task_id].has_subtask:
                # if a group task, pass values from all sub-tasks
                arg_value = {}
                base_len = len(task_id) + 1 # length of base name string
                for sub_id in tasks_dict[task_id].task_dep:
                    name = sub_id[base_len:]
                    arg_value[name] = get_value(sub_id, key_name)
            else:
                arg_value = get_value(task_id, key_name)
            task.options[arg] = arg_value


    def select_task(self, node, tasks_dict):
        """Returns bool, task should be executed
         * side-effect: set task.options

        Tasks should be executed if they are not up-to-date.

        Tasks that cointains setup-tasks must be selected twice,
        so it gives chance for dependency tasks to be executed after
        checking it is not up-to-date.
        """
        task = node.task

        # if run_status is not None, it was already calculated
        if node.run_status is None:

            self.reporter.get_status(task)

            # check if task should be ignored (user controlled)
            if node.ignored_deps or self.dep_manager.status_is_ignore(task):
                node.run_status = 'ignore'
                self.reporter.skip_ignore(task)
                return False

            # check task_deps
            if node.bad_deps:
                bad_str = " ".join(n.task.name for n in node.bad_deps)
                self._handle_task_error(node, UnmetDependency(bad_str))
                return False

            # check if task is up-to-date
            try:
                node.run_status = self.dep_manager.get_status(task, tasks_dict)
            except Exception as exception:
                msg = "ERROR: Task '%s' checking dependencies" % task.name
                dep_error = DependencyError(msg, exception)
                self._handle_task_error(node, dep_error)
                return False

            if not self.always_execute:
                # if task is up-to-date skip it
                if node.run_status == 'up-to-date':
                    self.reporter.skip_uptodate(task)
                    task.values = self.dep_manager.get_values(task.name)
                    return False

            if task.setup_tasks:
                # dont execute now, execute setup first...
                return False
        else:
            # sanity checks
            assert node.run_status == 'run', \
                "%s:%s" % (task.name, node.run_status)
            assert task.setup_tasks

        try:
            self._get_task_args(task, tasks_dict)
        except Exception as exception:
            msg = ("ERROR getting value for argument\n" + str(exception))
            self._handle_task_error(node, DependencyError(msg))
            return False

        return True


    def execute_task(self, task):
        """execute task's actions"""
        # register cleanup/teardown
        if task.teardown:
            self.teardown_list.append(task)

        # finally execute it!
        self.reporter.execute_task(task)
        return task.execute(sys.stdout, sys.stderr, self.verbosity)


    def process_task_result(self, node, catched_excp):
        """handles result"""
        task = node.task
        # save execution successful
        if catched_excp is None:
            node.run_status = "successful"
            task.save_extra_values()
            self.dep_manager.save_success(task)
            self.reporter.add_success(task)
        # task error
        else:
            self._handle_task_error(node, catched_excp)


    def run_tasks(self, task_dispatcher):
        """This will actually run/execute the tasks.
        It will check file dependencies to decide if task should be executed
        and save info on successful runs.
        It also deals with output to stdout/stderr.

        @param task_dispatcher: L{TaskDispacher}
        """
        node = None
        while True:
            if self._stop_running:
                break

            try:
                node = task_dispatcher.generator.send(node)
            except StopIteration:
                break

            if not self.select_task(node, task_dispatcher.tasks):
                continue

            catched_excp = self.execute_task(node.task)
            self.process_task_result(node, catched_excp)


    def teardown(self):
        """run teardown from all tasks"""
        for task in reversed(self.teardown_list):
            self.reporter.teardown_task(task)
            catched = task.execute_teardown(sys.stdout, sys.stderr,
                                            self.verbosity)
            if catched:
                msg = "ERROR: task '%s' teardown action" % task.name
                error = SetupError(msg, catched)
                self.reporter.cleanup_error(error)


    def finish(self):
        """finish running tasks"""
        # flush update dependencies
        self.dep_manager.close()
        self.teardown()

        # report final results
        self.reporter.complete_run()
        return self.final_result


    def run_all(self, task_dispatcher):
        """entry point to run tasks
        @ivar task_dispatcher (TaskDispatcher)
        """
        try:
            if hasattr(self.reporter, 'initialize'):
                self.reporter.initialize(task_dispatcher.tasks)
            self.run_tasks(task_dispatcher)
        except InvalidTask as exception:
            self.reporter.runtime_error(str(exception))
            self.final_result = ERROR
        finally:
            self.finish()
        return self.final_result



# JobXXX objects send from main process to sub-process for execution
class JobHold(object):
    """Indicates there is no task ready to be executed"""
    type = object()

class JobTask(object):
    """Contains a Task object"""
    type = object()
    def __init__(self, task):
        self.name = task.name
        try:
            self.task_pickle = pickle.dumps(task)
        except pickle.PicklingError as excp:
            msg = """Error on Task: `{}`.
Task created at execution time that has an attribute than can not be pickled,
so not feasible to be used with multi-processing. To fix this issue make sure
the task is pickable or just do not use multi-processing execution.

Original exception {}: {}
"""
            raise InvalidTask(msg.format(self.name, excp.__class__, excp))

class JobTaskPickle(object):
    """dict of Task object excluding attributes that might be unpicklable"""
    type = object()
    def __init__(self, task):
        self.task_dict = task.pickle_safe_dict() # actually a dict to be pickled
    @property
    def name(self):
        return self.task_dict['name']


class MReporter(object):
    """send reported messages to master process

    puts a dictionary {'name': <task-name>,
                       'reporter': <reporter-method-name>}
    on runner's 'result_q'
    """
    def __init__(self, runner, original_reporter):
        self.runner = runner
        self.original_reporter = original_reporter

    def __getattr__(self, method_name):
        """substitute any reporter method with a dispatching method"""
        if not hasattr(self.original_reporter, method_name):
            raise AttributeError(method_name)
        def rep_method(task):
            self.runner.result_q.put({'name':task.name,
                                      'reporter':method_name})
        return rep_method

    def complete_run(self):
        """ignore this on MReporter"""
        pass


class MRunner(Runner):
    """MultiProcessing Runner """
    Queue = staticmethod(MQueue)
    Child = staticmethod(Process)

    @staticmethod
    def available():
        """check if multiprocessing module is available"""
        # see: https://bitbucket.org/schettino72/doit/issue/17
        #      http://bugs.python.org/issue3770
        # not available on BSD systens
        try:
            import multiprocessing.synchronize
            multiprocessing # pyflakes
        except ImportError: # pragma: no cover
            return False
        else:
            return True

    def __init__(self, dep_manager, reporter,
                 continue_=False, always_execute=False,
                 verbosity=0, num_process=1):
        Runner.__init__(self, dep_manager, reporter, continue_=continue_,
                        always_execute=always_execute, verbosity=verbosity)
        self.num_process = num_process

        self.free_proc = 0   # number of free process
        self.task_dispatcher = None # TaskDispatcher retrieve tasks
        self.tasks = None    # dict of task instances by name
        self.result_q = None

    def get_next_job(self, completed):
        """get next task to be dispatched to sub-process

        On MP needs to check if the dependencies finished its execution
        @returns : - None -> no more tasks to be executed
                   - JobXXX
        """
        if self._stop_running:
            return None # gentle stop
        node = completed
        while True:
            # get next task from controller
            try:
                node = self.task_dispatcher.generator.send(node)
                if node == "hold on":
                    self.free_proc += 1
                    return JobHold()
            # no more tasks from controller...
            except StopIteration:
                # ... terminate one sub process if no other task waiting
                return None

            # send a task to be executed
            if self.select_task(node, self.tasks):
                # If sub-process already contains the Task object send
                # only safe pickle data, otherwise send whole object.
                task = node.task
                if task.loader is DelayedLoaded and self.Child == Process:
                    return JobTask(task)
                else:
                    return JobTaskPickle(task)


    def _run_tasks_init(self, task_dispatcher):
        """initialization for run_tasks"""
        self.task_dispatcher = task_dispatcher
        self.tasks = task_dispatcher.tasks


    def _run_start_processes(self, job_q, result_q):
        """create and start sub-processes
        @param job_q: (multiprocessing.Queue) tasks to be executed
        @param result_q: (multiprocessing.Queue) collect task results
        @return list of Process
        """
        proc_list = []
        for _ in xrange(self.num_process):
            next_job = self.get_next_job(None)
            if next_job is None:
                break # do not start more processes than tasks
            job_q.put(next_job)
            process = self.Child(
                target=self.execute_task_subprocess,
                args=(job_q, result_q))
            process.start()
            proc_list.append(process)
        return proc_list

    def _process_result(self, node, task, result):
        """process result received from sub-process"""
        if 'failure' in result:
            catched_excp = result['failure']
        else:
            # success set values taken from subprocess result
            catched_excp = None
            task.update_from_pickle(result['task'])
            for action, output in zip(task.actions, result['out']):
                action.out = output
            for action, output in zip(task.actions, result['err']):
                action.err = output
        self.process_task_result(node, catched_excp)


    def run_tasks(self, task_dispatcher):
        """controls subprocesses task dispatching and result collection
        """
        # result queue - result collected from sub-processes
        result_q = self.Queue()
        # task queue - tasks ready to be dispatched to sub-processes
        job_q = self.Queue()
        self._run_tasks_init(task_dispatcher)
        try:
            proc_list = self._run_start_processes(job_q, result_q)
        except pickle.PicklingError as exc:
            raise InvalidTask(repr(exc))

        # wait for all processes terminate
        proc_count = len(proc_list)
        try:
            while proc_count:
                # wait until there is a result to be consumed
                result = result_q.get()

                if 'exit' in result:
                    raise result['exit'](result['exception'])
                node = task_dispatcher.nodes[result['name']]
                task = node.task
                if 'reporter' in result:
                    getattr(self.reporter, result['reporter'])(task)
                    continue
                self._process_result(node, task, result)

                # update num free process
                free_proc = self.free_proc + 1
                self.free_proc = 0
                # tries to get as many tasks as free process
                completed = node
                for _ in range(free_proc):
                    next_job = self.get_next_job(completed)
                    completed = None
                    if next_job is None:
                        proc_count -= 1
                    job_q.put(next_job)
                # check for cyclic dependencies
                assert len(proc_list) > self.free_proc
        except Exception:
            if self.Child == Process:
                for proc in proc_list:
                    proc.terminate()
            raise
        # we are done, join all process
        for proc in proc_list:
            proc.join()

        # get teardown results
        while not result_q.empty(): # safe because subprocess joined
            result = result_q.get()
            assert 'reporter' in result
            task = task_dispatcher.tasks[result['name']]
            getattr(self.reporter, result['reporter'])(task)


    def execute_task_subprocess(self, job_q, result_q):
        """executed on child processes
        @param job_q: task queue,
            * None elements indicate process can terminate
            * JobHold indicate process should wait for next task
            * JobTask / JobTaskPickle task to be executed
        """
        self.result_q = result_q
        if self.Child == Process:
            self.reporter = MReporter(self, self.reporter)
        try:
            while True:
                job = job_q.get()

                if job is None:
                    self.teardown()
                    return # no more tasks to execute finish this process

                # job is an incomplete Task obj when pickled, attrbiutes
                # that might contain unpickleble data were removed.
                # so we need to get task from this process and update it
                # to get dynamic task attributes.
                if job.type is JobTaskPickle.type:
                    task = self.tasks[job.name]
                    if self.Child == Process: # pragma: no cover ...
                        # ... actually covered but subprocess doesnt get it.
                        task.update_from_pickle(job.task_dict)

                elif job.type is JobTask.type:
                    task = pickle.loads(job.task_pickle)

                # do nothing. this is used to start the subprocess even
                # if no task is available when process is created.
                else:
                    assert job.type is JobHold.type
                    continue # pragma: no cover

                result = {'name': task.name}
                t_result = self.execute_task(task)

                if t_result is None:
                    result['task'] = task.pickle_safe_dict()
                    result['out'] = [a.out for a in task.actions]
                    result['err'] = [a.err for a in task.actions]
                else:
                    result['failure'] = t_result
                result_q.put(result)
        except (SystemExit, KeyboardInterrupt, Exception) as exception:
            # error, blow-up everything. send exception info to master process
            result_q.put({
                'exit': exception.__class__,
                'exception': str(exception)})


class MThreadRunner(MRunner):
    """Parallel runner using threads"""
    Queue = staticmethod(queue.Queue)
    class DaemonThread(Thread):
        """daemon thread to make sure process is terminated if there is
        an uncatch exception and threads are not correctly joined.
        """
        def __init__(self, *args, **kwargs):
            Thread.__init__(self, *args, **kwargs)
            self.daemon = True
    Child = staticmethod(DaemonThread)

    @staticmethod
    def available():
        return True
